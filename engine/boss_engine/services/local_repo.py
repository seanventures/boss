"""LOCAL-FIRST PROJECTS (BOSSAPP-915): a project's code in a git repository BOSS owns, on this
machine, with no GitHub at all — until a person chooses to publish it.

The operator: "I really wanted to build things locally before taking to git later.. if people
want to use git from beginning it is great, but we should provide option to build locally and
also offline use cases."

This module is the git half (no database): one BARE repository per local project under the
install's repo root (`BOSS_LOCAL_REPO_ROOT`, a named volume in the compose mounted in the API and
the worker), plus bare MIRRORS of GitHub repositories that a build falls back to while GitHub is
down (BOSSAPP-922). Everything is real git — `git init --bare`, real commits, real merges, real
pushes — so the same history publishes to GitHub later unchanged.

    <root>/<project_id>/<name>.git          a local project's repository (BOSSAPP-918)
    <root>/_mirrors/<owner>/<repo>.git      a GitHub repository's local mirror (BOSSAPP-922)

A local repository is named `local_<project_id>/<name>` wherever BOSS names repositories
(bindings, specs, change sets). GitHub owner names cannot contain "_", so a local name can never
be mistaken for a GitHub repository, and the owner alone says where the bare repository is.

The pipeline never calls this module for a stage's commit directly: `local_repo_host.LocalGitAdapter`
puts it behind the same interface as `GitHubAdapter`, so a build has ONE code path whichever host
holds its code.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

OWNER_PREFIX = "local_"
MIRRORS = "_mirrors"
DEFAULT_BRANCH = "main"
MARKER = "boss-local.json"
AUTHOR_NAME = "BOSS"
AUTHOR_EMAIL = "boss-agent@users.noreply.github.com"
_NAME = re.compile(r"[^a-z0-9._-]+")
_ZERO = "0" * 40

#: What an import of a folder without history never copies: dependencies, build output, and a
#: local `.env` (a secret file is not code; `.env.example` is kept).
IMPORT_EXCLUDES = ("node_modules/", ".venv/", "venv/", "__pycache__/", ".pytest_cache/",
                   "dist/", "build/", ".next/", "target/", ".DS_Store", ".env", ".env.local",
                   "*.pyc")

#: A new local project's first commit.
GITIGNORE = "node_modules/\n.venv/\n__pycache__/\ndist/\nbuild/\n.env\n.env.local\n*.pyc\n.DS_Store\n"


class LocalRepoError(RuntimeError):
    """A refusal or failure, in words a person can act on."""


class LocalMergeConflict(LocalRepoError):
    """The branch does not merge cleanly into the default branch."""


# ── where things are ───────────────────────────────────────────────────────────────────────────

def root(settings: Any = None) -> Path:
    """The BOSS-owned directory that holds every local repository and mirror."""
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    configured = str(getattr(settings, "local_repo_root", "") or "").strip()
    if configured:
        return Path(configured)
    return Path(settings.artifact_root) / "repos"


def owner_for(project_id: str) -> str:
    return f"{OWNER_PREFIX}{project_id}"


def is_local_owner(owner: Any) -> bool:
    return str(owner or "").startswith(OWNER_PREFIX)


def is_local(full_name_or_owner: Any) -> bool:
    return is_local_owner(str(full_name_or_owner or "").split("/", 1)[0])


def project_of(owner: str) -> str:
    if not is_local_owner(owner):
        raise LocalRepoError(f"{owner!r} is not a local repository owner.")
    return str(owner)[len(OWNER_PREFIX):]


def clean_name(name: Any) -> str:
    """A repository name from a person's words: lower case, letters, digits, '.', '_', '-'."""
    slug = _NAME.sub("-", str(name or "").strip().lower()).strip("-._")
    return slug[:80] or "app"


def _safe_part(value: str, what: str) -> str:
    value = str(value or "")
    if not value or value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
        raise LocalRepoError(f"That is not a usable {what}: {value!r}.")
    return value


def bare_path(owner: str, repo: str, settings: Any = None) -> Path:
    """Where `owner/repo` lives on disk: a local project's repository, or a GitHub mirror."""
    repo = _safe_part(repo, "repository name")
    if is_local_owner(owner):
        return root(settings) / _safe_part(project_of(owner), "project") / f"{repo}.git"
    return mirror_path(owner, repo, settings)


def project_dir(project_id: str, settings: Any = None) -> Path:
    return root(settings) / _safe_part(project_id, "project")


def mirror_path(owner: str, repo: str, settings: Any = None) -> Path:
    return root(settings) / MIRRORS / _safe_part(owner, "owner") / f"{_safe_part(repo, 'repository name')}.git"


def file_url(path: Union[str, Path]) -> str:
    return "file://" + str(Path(path).resolve())


def exists(owner: str, repo: str, settings: Any = None) -> bool:
    try:
        return (bare_path(owner, repo, settings) / "HEAD").is_file()
    except LocalRepoError:
        return False


# ── git, with nothing of the machine's own configuration ─────────────────────────────────────

def _env(extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH") or os.defpath, "HOME": tempfile.gettempdir(),
           "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": AUTHOR_NAME, "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
           "GIT_COMMITTER_NAME": AUTHOR_NAME, "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL}
    env.update(extra or {})
    return env


def git(args: list[str], *, git_dir: Optional[Path] = None, cwd: Optional[Path] = None,
        input: Optional[bytes] = None, env: Optional[Mapping[str, str]] = None,
        check: bool = True, timeout: int = 300, raw: bool = False) -> Any:
    """One git command. Returns stdout (text, stripped of its last newline; bytes when `raw`)."""
    argv = ["git"]
    if git_dir is not None:
        argv += ["--git-dir", str(git_dir)]
    argv += list(args)
    try:
        done = subprocess.run(argv, cwd=str(cwd) if cwd else None, input=input,
                              capture_output=True, env=_env(env), timeout=timeout)
    except FileNotFoundError as error:
        raise LocalRepoError("git is not installed on this machine.") from error
    except subprocess.TimeoutExpired as error:
        raise LocalRepoError(f"git {args[0]} took longer than {timeout}s.") from error
    if check and done.returncode != 0:
        said = (done.stderr or b"").decode("utf-8", "replace").strip()[-400:]
        raise LocalRepoError(f"git {args[0]} failed: {said or f'exit {done.returncode}'}")
    if raw:
        return done.stdout
    out = (done.stdout or b"").decode("utf-8", "replace")
    return out[:-1] if out.endswith("\n") else out


def git_available() -> Optional[str]:
    """git's version, or None when git is not on this machine."""
    try:
        return git(["--version"], timeout=15)
    except LocalRepoError:
        return None


def rev_parse(git_dir: Path, ref: str) -> Optional[str]:
    out = git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], git_dir=git_dir, check=False)
    return out.strip() or None


def branch_sha(git_dir: Path, branch: str) -> Optional[str]:
    return rev_parse(git_dir, f"refs/heads/{branch}")


def branch_exists(git_dir: Path, branch: str) -> bool:
    return branch_sha(git_dir, branch) is not None


def branches(git_dir: Path) -> list[str]:
    out = git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], git_dir=git_dir)
    return [line for line in out.splitlines() if line.strip()]


def default_branch(git_dir: Path) -> str:
    out = git(["symbolic-ref", "--quiet", "HEAD"], git_dir=git_dir, check=False).strip()
    return out[len("refs/heads/"):] if out.startswith("refs/heads/") else DEFAULT_BRANCH


def init_bare(path: Path, *, default: str = DEFAULT_BRANCH, marker: Optional[dict] = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not (path / "HEAD").is_file():
        git(["init", "--bare", "--quiet", str(path)])
        git(["symbolic-ref", "HEAD", f"refs/heads/{default}"], git_dir=path)
    if marker is not None:
        (path / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return path


def marker_of(path: Path) -> dict[str, Any]:
    try:
        return json.loads((path / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_marker(path: Path, **fields: Any) -> dict[str, Any]:
    data = marker_of(path)
    data.update(fields)
    (path / MARKER).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


# ── commits, without a checkout ──────────────────────────────────────────────────────────────

Content = Union[str, bytes, None]


def commit_files(git_dir: Path, branch: str, files: Mapping[str, Content], message: str, *,
                 parent: Optional[str] = None) -> str:
    """ONE real commit on `branch` that writes (or, for None, deletes) each path, made straight in
    the bare repository with a private index. Returns the branch's new head; a commit that would
    change nothing is not made (the head is returned unchanged)."""
    tip = branch_sha(git_dir, branch) or (rev_parse(git_dir, parent) if parent else None)
    with tempfile.TemporaryDirectory(prefix="boss-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if tip:
            git(["read-tree", tip], git_dir=git_dir, env=env)
        else:
            git(["read-tree", "--empty"], git_dir=git_dir, env=env)
        for path, content in files.items():
            rel = str(path).lstrip("/")
            if not rel or ".." in Path(rel).parts:
                raise LocalRepoError(f"Refusing a path outside the repository: {path!r}.")
            if content is None:
                git(["update-index", "--force-remove", "--", rel], git_dir=git_dir, env=env)
                continue
            data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
            blob = git(["hash-object", "-w", "--stdin"], git_dir=git_dir, input=data).strip()
            staged = git(["ls-files", "-s", "--", rel], git_dir=git_dir, env=env).split()
            mode = staged[0] if staged and staged[0] in ("100644", "100755") else "100644"
            git(["update-index", "--add", "--cacheinfo", f"{mode},{blob},{rel}"],
                git_dir=git_dir, env=env)
        tree = git(["write-tree"], git_dir=git_dir, env=env).strip()
    if tip and git(["rev-parse", f"{tip}^{{tree}}"], git_dir=git_dir).strip() == tree:
        if not branch_sha(git_dir, branch):
            git(["update-ref", f"refs/heads/{branch}", tip, _ZERO], git_dir=git_dir)
        return tip
    args = ["commit-tree", tree, "-m", message or "BOSS: update"]
    if tip:
        args += ["-p", tip]
    sha = git(args, git_dir=git_dir).strip()
    old = branch_sha(git_dir, branch) or _ZERO
    git(["update-ref", f"refs/heads/{branch}", sha, old], git_dir=git_dir)
    return sha


def ensure_branch(git_dir: Path, branch: str, base: Optional[str] = None) -> str:
    """The branch's head, cutting it from `base` (else the default branch) when it is new."""
    sha = branch_sha(git_dir, branch)
    if sha:
        return sha
    source = base or default_branch(git_dir)
    base_sha = branch_sha(git_dir, source) or rev_parse(git_dir, source)
    if not base_sha:
        raise LocalRepoError(f"There is no branch {source!r} to start {branch!r} from.")
    git(["update-ref", f"refs/heads/{branch}", base_sha, _ZERO], git_dir=git_dir)
    return base_sha


def read_file(git_dir: Path, ref: str, path: str) -> Optional[str]:
    out = subprocess.run(["git", "--git-dir", str(git_dir), "cat-file", "-p", f"{ref}:{path.lstrip('/')}"],
                         capture_output=True, env=_env(), timeout=60)
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", "replace")


def list_paths(git_dir: Path, ref: str, *, limit: int = 20000) -> list[str]:
    if not rev_parse(git_dir, ref):
        return []
    out = git(["ls-tree", "-r", "--name-only", ref], git_dir=git_dir)
    return [p for p in out.splitlines() if p][:limit]


def merge_base(git_dir: Path, a: str, b: str) -> Optional[str]:
    out = git(["merge-base", a, b], git_dir=git_dir, check=False).strip()
    return out or None


def diff(git_dir: Path, base: str, head: str) -> str:
    """The change a branch makes against its base (three dots: since they diverged)."""
    return git(["diff", "--no-color", f"{base}...{head}"], git_dir=git_dir)


def changed_files(git_dir: Path, base: str, head: str) -> list[dict[str, Any]]:
    status = {}
    for line in git(["diff", "--name-status", f"{base}...{head}"], git_dir=git_dir).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            status[parts[-1]] = {"A": "added", "D": "removed", "M": "modified"}.get(parts[0][:1], "renamed")
    out = []
    for line in git(["diff", "--numstat", f"{base}...{head}"], git_dir=git_dir).splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            add, rem, path = parts
            out.append({"path": path, "status": status.get(path, "modified"),
                        "additions": int(add) if add.isdigit() else 0,
                        "deletions": int(rem) if rem.isdigit() else 0})
    return out


def commits_between(git_dir: Path, base: str, head: str, *, limit: int = 200) -> list[dict[str, str]]:
    out = git(["log", f"--max-count={limit}", "--format=%H%x09%s", f"{base}..{head}"], git_dir=git_dir)
    return [{"sha": line.split("\t", 1)[0], "message": line.split("\t", 1)[-1]}
            for line in out.splitlines() if line.strip()]


@contextlib.contextmanager
def worktree(git_dir: Path, ref: Optional[str] = None):
    """A throwaway checkout of the bare repository (a real clone over file://), removed after."""
    tmp = Path(tempfile.mkdtemp(prefix="boss-local-wt-"))
    try:
        args = ["clone", "--quiet", "--no-hardlinks"]
        if ref:
            args += ["--branch", ref]
        git(args + [file_url(git_dir), str(tmp / "w")])
        yield tmp / "w"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def merge(git_dir: Path, head: str, base: str, message: str, *, method: str = "merge") -> str:
    """Merge branch `head` into branch `base` (fast-forward when possible, else a merge commit;
    `squash` makes one commit). Returns the new head of `base`. A conflict changes nothing and
    raises LocalMergeConflict with the conflicting paths."""
    head_sha = branch_sha(git_dir, head)
    base_sha = branch_sha(git_dir, base)
    if not head_sha:
        raise LocalRepoError(f"There is no branch {head!r} to merge.")
    if not base_sha:
        raise LocalRepoError(f"There is no branch {base!r} to merge into.")
    if merge_base(git_dir, head_sha, base_sha) == head_sha:
        return base_sha                                   # already in
    if method != "squash" and merge_base(git_dir, head_sha, base_sha) == base_sha:
        git(["update-ref", f"refs/heads/{base}", head_sha, base_sha], git_dir=git_dir)
        return head_sha                                   # fast-forward
    with worktree(git_dir, base) as wt:
        git(["fetch", "--quiet", "origin", f"refs/heads/{head}:refs/remotes/origin/{head}"], cwd=wt)
        if method == "squash":
            done = subprocess.run(["git", "merge", "--squash", f"origin/{head}"], cwd=str(wt),
                                  capture_output=True, env=_env(), timeout=300)
        else:
            done = subprocess.run(["git", "merge", "--no-ff", "-m", message, f"origin/{head}"],
                                  cwd=str(wt), capture_output=True, env=_env(), timeout=300)
        if done.returncode != 0:
            conflicted = git(["diff", "--name-only", "--diff-filter=U"], cwd=wt, check=False)
            raise LocalMergeConflict(
                f"{head} does not merge cleanly into {base}"
                + (f": {', '.join(conflicted.split()[:8])} conflict." if conflicted.strip() else
                   f": {(done.stderr or b'').decode('utf-8', 'replace').strip()[-200:]}"))
        if method == "squash":
            git(["commit", "--quiet", "-m", message], cwd=wt)
        git(["push", "--quiet", "origin", f"HEAD:refs/heads/{base}"], cwd=wt)
    return branch_sha(git_dir, base) or ""


# ── a local project's repository ─────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initial_files(title: str, description: str = "") -> dict[str, str]:
    readme = f"# {title.strip() or 'New project'}\n"
    if description.strip():
        readme += f"\n{description.strip()}\n"
    readme += ("\nThis project's code lives on this machine, in a git repository BOSS keeps. "
               "Publish it to GitHub whenever you are ready; its whole history goes with it.\n")
    return {"README.md": readme, ".gitignore": GITIGNORE}


def create(project_id: str, name: str, *, settings: Any = None, title: str = "",
           description: str = "", files: Optional[Mapping[str, Content]] = None,
           default: str = DEFAULT_BRANCH) -> dict[str, Any]:
    """A new local project's bare repository with an initial commit (README, .gitignore).
    Idempotent: an existing repository is returned as it is."""
    owner, repo = owner_for(project_id), clean_name(name)
    path = bare_path(owner, repo, settings)
    if exists(owner, repo, settings) and branch_sha(path, default_branch(path)):
        return describe(owner, repo, settings)
    init_bare(path, default=default, marker={"project_id": project_id, "name": repo,
                                             "created_at": _now(), "origin": "new"})
    commit_files(path, default, files or initial_files(title or name, description),
                 "Initial commit (BOSS)")
    return describe(owner, repo, settings)


def describe(owner: str, repo: str, settings: Any = None) -> dict[str, Any]:
    path = bare_path(owner, repo, settings)
    default = default_branch(path)
    head = branch_sha(path, default)
    return {"host": "local", "owner": owner, "repo": repo, "full_name": f"{owner}/{repo}",
            "path": str(path), "default_branch": default, "head_sha": head,
            "branches": branches(path) if head else [], "marker": marker_of(path)}


def delete_project_repos(project_id: str, settings: Any = None) -> bool:
    """Remove every bare repository of a project (Delete project). True when something went."""
    target = project_dir(project_id, settings)
    base = root(settings).resolve()
    try:
        resolved = target.resolve()
    except OSError:
        return False
    if base not in resolved.parents or resolved.name in (MIRRORS, ""):
        raise LocalRepoError("Refusing to delete outside the local repository root.")
    if not resolved.exists():
        return False
    shutil.rmtree(resolved)
    return True


# ── importing code that already exists ───────────────────────────────────────────────────────

def _has_history(source: Path) -> bool:
    if not (source / ".git").exists():
        return False
    return bool(git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=source, check=False).strip())


def import_tree(project_id: str, name: str, source: Path, *, settings: Any = None,
                keep_history: bool = True, default: Optional[str] = None,
                origin: str = "folder") -> dict[str, Any]:
    """A local repository from an existing folder. A folder with git history keeps all of it
    (every branch and tag; its remotes are not copied, and the folder itself is never written);
    a folder without history becomes one initial commit of its files, without dependencies,
    build output or a local .env."""
    source = Path(source)
    if not source.is_dir():
        raise LocalRepoError(f"There is no folder {source}.")
    owner, repo = owner_for(project_id), clean_name(name)
    path = bare_path(owner, repo, settings)
    if exists(owner, repo, settings):
        raise LocalRepoError(f"{owner}/{repo} already exists.")
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = {"project_id": project_id, "name": repo, "created_at": _now(), "origin": origin,
              "source": str(source)}
    if keep_history and _has_history(source):
        remote = git(["remote", "get-url", "origin"], cwd=source, check=False).strip()
        git(["clone", "--bare", "--quiet", "--no-hardlinks", file_url(source), str(path)])
        git(["remote", "remove", "origin"], git_dir=path, check=False)
        if default:
            git(["symbolic-ref", "HEAD", f"refs/heads/{default}"], git_dir=path)
        marker.update({"history": "kept", "source_remote": re.sub(r"^(\w+://)[^/@]*@", r"\1", remote)
                       if remote else None})
        (path / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        return describe(owner, repo, settings)
    init_bare(path, default=default or DEFAULT_BRANCH, marker={**marker, "history": "new"})
    with tempfile.TemporaryDirectory(prefix="boss-import-") as tmp:
        excludes = Path(tmp) / "exclude"
        excludes.write_text("\n".join(IMPORT_EXCLUDES) + "\n", encoding="utf-8")
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index"), "GIT_WORK_TREE": str(source)}
        git(["-c", f"core.excludesFile={excludes}", "add", "-A", "."], git_dir=path, cwd=source, env=env)
        tree = git(["write-tree"], git_dir=path, env=env).strip()
    sha = git(["commit-tree", tree, "-m", "Initial import (BOSS)"], git_dir=path).strip()
    git(["update-ref", f"refs/heads/{default or DEFAULT_BRANCH}", sha, _ZERO], git_dir=path)
    return describe(owner, repo, settings)


# ── pushing and mirroring (publish, and GitHub outages) ──────────────────────────────────────

def push(git_dir: Path, url: str, refspecs: Iterable[str], *, env: Optional[Mapping[str, str]] = None,
         timeout: int = 600) -> str:
    return git(["push", "--porcelain", url, *refspecs], git_dir=git_dir, env=env, timeout=timeout)


def ensure_mirror(owner: str, repo: str, url: str, *, settings: Any = None,
                  env: Optional[Mapping[str, str]] = None, timeout: int = 300) -> Path:
    """The GitHub repository's bare mirror here, brought up to date (every branch and tag).
    Raises when GitHub cannot be reached; the mirror keeps what it had."""
    path = mirror_path(owner, repo, settings)
    if not (path / "HEAD").is_file():
        init_bare(path, marker={"mirror_of": f"{owner}/{repo}", "created_at": _now()})
    git(["fetch", "--quiet", "--prune", url, "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"],
        git_dir=path, env=env, timeout=timeout)
    remote_head = git(["ls-remote", "--symref", url, "HEAD"], git_dir=path, env=env, check=False,
                      timeout=60)
    found = re.search(r"ref: refs/heads/(\S+)\s+HEAD", remote_head or "")
    if found:
        git(["symbolic-ref", "HEAD", f"refs/heads/{found.group(1)}"], git_dir=path)
    update_marker(path, fetched_at=_now())
    return path


__all__ = [
    "LocalMergeConflict", "LocalRepoError", "OWNER_PREFIX", "bare_path", "branch_exists",
    "branch_sha", "branches", "changed_files", "clean_name", "commit_files", "commits_between",
    "create", "default_branch", "delete_project_repos", "describe", "diff", "ensure_branch",
    "ensure_mirror", "exists", "file_url", "git", "git_available", "import_tree", "init_bare",
    "is_local", "is_local_owner", "list_paths", "merge", "mirror_path", "owner_for",
    "project_of", "push", "read_file", "rev_parse", "root",
]
