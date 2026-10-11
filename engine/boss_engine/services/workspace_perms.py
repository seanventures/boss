"""A RUN'S WORKSPACE IS ALWAYS WRITABLE BY THE RUN AND BY BOSS.

crun_966aa44e (HelpDesk, login-oauth template) died after an hour on
`[Errno 13] Permission denied: '.../helpdesk-ai/auth/config.mjs'`. The template's reference files
are written read-only on purpose (templates/run_ref.py: files 0444, dirs 0555, OUTSIDE the
workspace). The coder `cp -r`-ed them into the workspace, the copies kept 0444/0555, and the next
BOSS-side write to that path (the mutation probe putting the implementation back) raised a bare
PermissionError that failed the whole build as `infra`.

The rule, in one place:

* `normalize(root)` — every file under a workspace is owner- (and, for files the sandbox uid owns,
  group-) writable; every directory owner/group rwx. `node_modules` and `.git` are skipped, so it
  is cheap enough to run after every agent turn (`tidies_workspace`, on every agent's `run_role`).
  Files the run's sandbox uid owns can only be chmod-ed BY that uid, so those go through
  boss-sandbox-exec as the run's uid; what BOSS owns it chmods itself.
* `make_writable(path)` — the lazy form, for one path and its parent: what a BOSS-side write does
  on EACCES before retrying once (`write_text`).

The template reference itself stays read-only: it lives outside the workspace and nothing here
is ever pointed at it.
"""
from __future__ import annotations

import contextlib
import functools
import logging
import os
import stat
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("boss.workspace_perms")

#: Never walked: huge, and not the agent's sources (git's own objects are 0444 by design).
SKIP_DIRS = frozenset({"node_modules", ".git"})

#: What BOSS needs on a path it owns, and on one the run's uid owns (BOSS writes it as group boss).
_OWN_FILE, _OWN_DIR = 0o200, 0o700
_GROUP_FILE, _GROUP_DIR = 0o220, 0o770

#: Run AS the run's uid: make what it owns under "$1" group-writable, skipping the same dirs.
_FIND_FIX = (
    'find "$1" \\( -name node_modules -o -name .git \\) -prune -o -user "$(id -u)" '
    '\\( \\( -type d ! -perm -0770 -exec chmod ug+rwx {} + \\) '
    '-o \\( -type f ! -perm -0660 -exec chmod ug+rw {} + \\) \\) 2>/dev/null; true'
)
#: As the run's uid: every file and directory it owns under "$1" readable (and dirs enterable).
_READABLE = 'find "$1" -user "$(id -u)" ! -perm -o+r -exec chmod a+rX {} + 2>/dev/null; true'
#: The lazy form: exactly the named paths (a file and its parent), as the run's uid.
_CHMOD_PATHS = 'for p in "$@"; do if [ -d "$p" ]; then chmod ug+rwx "$p"; else chmod ug+rw "$p"; fi; done 2>/dev/null; true'


def _identity(root: Path) -> Any:
    """The run's sandbox identity when `root` is inside its directory, else None."""
    try:
        from . import sandbox_identity

        ident = sandbox_identity.current()
        if ident is None:
            return None
        Path(root).resolve().relative_to(Path(ident.run_dir).resolve())
        return ident
    except Exception:  # noqa: BLE001 — not isolated / not inside the run: in-process only
        return None


def _as_run_uid(ident: Any, script: str, *args: str) -> bool:
    """Run `script` as the run's sandbox uid (boss-sandbox-exec), never as BOSS. Never raises."""
    try:
        from . import owned_process, sandbox_identity

        helper = sandbox_identity.helper_path()
        if helper is None:
            return False
        proc = owned_process.run([helper, "run", str(ident.uid), "--", "/bin/sh", "-c", script,
                                  "sh", *args], capture_output=True, timeout=120,
                                 sandboxed=False, cwd="/")
        return proc.returncode == 0
    except Exception as exc:  # noqa: BLE001
        log.warning("workspace permissions: helper chmod failed: %s", type(exc).__name__)
        return False


def _fix_one(path: Path, st: os.stat_result, me: int) -> Optional[bool]:
    """chmod one entry BOSS owns. True fixed, False fine already, None: another uid owns it and
    it needs fixing (only that uid can)."""
    is_dir = stat.S_ISDIR(st.st_mode)
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid == me:
        want = _OWN_DIR if is_dir else _OWN_FILE
        if mode & want == want:
            return False
        try:
            os.chmod(path, mode | want)
            return True
        except OSError:
            return None
    want = _GROUP_DIR if is_dir else _GROUP_FILE
    return False if mode & want == want else None


def normalize(root: Any, *, readable_by_boss: bool = True) -> int:
    """Make the workspace at `root` writable again (see the module doc). Returns how many entries
    needed fixing. Never raises: a workspace BOSS cannot tidy is reported by the write that
    follows, not by this."""
    base = Path(root)
    try:
        if not base.is_dir():
            return 0
    except OSError:
        return 0
    me = os.geteuid()
    fixed, foreign = 0, False
    for dirpath, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        here = Path(dirpath)
        for name in [*dirnames, *filenames, ""] if dirpath == str(base) else [*dirnames, *filenames]:
            p = here / name if name else here
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                continue
            res = _fix_one(p, st, me)
            if res is None:
                foreign = True
                fixed += 1
            elif res:
                fixed += 1
        # A directory BOSS could not enter/list is walked as the run's uid below.
    ident = _identity(base) if foreign or readable_by_boss else None
    if foreign and ident is not None:
        _as_run_uid(ident, _FIND_FIX, str(base))
    if readable_by_boss and ident is not None:
        # EVERYTHING the run made is readable by BOSS, whatever its group: a tool that writes
        # 0600 / 0700 (or a file whose group is the run uid's own) left BOSS unable to read the
        # template's junit report, dist/ and node_modules/ (HelpDesk crun_8e9b3b0e, after five
        # earlier crashes of the same class). Others gain nothing: the run directory is
        # <uid>:boss 2770, so no other uid can enter it.
        _as_run_uid(ident, _READABLE, str(base))
    if fixed:
        log.info("workspace permissions: %d entr%s under %s made writable", fixed,
                 "y" if fixed == 1 else "ies", base)
    return fixed


def make_writable(path: Any, *, within: Any = None) -> None:
    """The lazy form: `path` (if it exists) and its parent directory. With `within`, nothing is
    touched unless the path resolves inside it (an agent-named path never reaches outside its
    workspace through a symlink). Never raises."""
    p = Path(path)
    if within is not None:
        try:
            p.parent.resolve().relative_to(Path(within).resolve())
        except (ValueError, OSError):
            return
    me = os.geteuid()
    foreign: list[str] = []
    for target in (p.parent, p):
        try:
            st = os.lstat(target)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        if _fix_one(target, st, me) is None:
            foreign.append(str(target))
    if foreign:
        ident = _identity(p.parent)
        if ident is not None:
            _as_run_uid(ident, _CHMOD_PATHS, *foreign)


def write_text(path: Any, content: str, *, encoding: str = "utf-8") -> None:
    """`Path.write_text` for a BOSS-side write into a run's workspace: on EACCES the path (and its
    parent) are made writable and the write is tried once more. A second failure raises."""
    p = Path(path)
    try:
        p.write_text(content, encoding=encoding)
    except PermissionError:
        make_writable(p)
        p.write_text(content, encoding=encoding)


def tidies_workspace(fn: Callable[..., Any]) -> Callable[..., Any]:
    """For an agent's `run_role(prompt, cwd, *, read_only=...)`: after every turn that could
    change files, the workspace is normalized, so nothing the agent copied in read-only can
    stop BOSS (or the next stage) from writing it."""

    @functools.wraps(fn)
    def run_role(self, prompt, cwd, *args, **kwargs):
        try:
            return fn(self, prompt, cwd, *args, **kwargs)
        finally:
            if not kwargs.get("read_only"):
                with contextlib.suppress(Exception):
                    normalize(cwd)

    run_role.__boss_tidies_workspace__ = True  # type: ignore[attr-defined]
    return run_role


def tidies_after_command(fn: Callable[..., Any]) -> Callable[..., Any]:
    """For a sandbox's `run(command, ...)`: after EVERY command (install, build, the test run),
    the workspace is normalized, as an agent turn's is. A Playwright run leaves test-results/
    readable only by the sandbox uid, and BOSS then crashed reading it three times in a row
    (node_modules/.bin, test-results/.last-run.json, test-results/go.mod)."""

    @functools.wraps(fn)
    def run(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        finally:
            with contextlib.suppress(Exception):
                if getattr(self, "path", None):
                    normalize(self.path)

    run.__boss_tidies_after_command__ = True  # type: ignore[attr-defined]
    return run
