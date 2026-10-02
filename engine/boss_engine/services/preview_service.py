"""Tier-1 "1-box" app previews.

After a code task produces a green PR, an operator wants to *see the working website*
before merging. This runs the built app from its branch as a managed, auto-expiring local
process on the dev box and hands back a live preview URL — zero extra compute.

Process-based (not containerized) for v1: simplest, works with loopback-binding apps on
macOS where container host-networking is awkward. Containerized isolation is the v2 hardening.
"""
from __future__ import annotations

import json
import shlex
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from ..config import Settings
from ..db import begin
from .code_task_service import load_code_task_run
from .core import emit_event, make_id
from .github_connections import github_token_for
from .execution_substrate import ExecutionSubstrate, normalize_build_substrate
from .sandbox_provider import MicroVMProvider

from .scheduling import low_priority

logger = logging.getLogger(__name__)

PREVIEW_ROOT = Path(os.environ.get("BOSS_PREVIEW_ROOT", "/tmp/boss-previews"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_loopback_url(url: Optional[str]) -> bool:
    if not url:
        return False
    lowered = url.strip().lower()
    return (
        "://127." in lowered
        or "://localhost" in lowered
        or "://[::1]" in lowered
        or "://0.0.0.0" in lowered
        or lowered.startswith("127.")
        or lowered.startswith("localhost")
        or lowered.startswith("[::1]")
        or lowered.startswith("0.0.0.0")
    )


def _preview_public_base(
    explicit: Optional[str] = None, *, settings: Optional[Settings] = None
) -> str:
    """Operator-facing origin for View URLs. Loopback bases are rejected (fail closed
    to relative gateway paths) so a mis-set BOSS_PREVIEW_PUBLIC_BASE cannot reintroduce
    127.0.0.1 into operator-facing URLs.

    An explicit ``public_base`` wins (and a loopback explicit value still fail-closes).
    Otherwise prefer ``settings.preview_public_base``, then ``BOSS_PREVIEW_PUBLIC_BASE``
    in the process environment so a Settings object constructed without the field
    (or with an empty default) cannot hide a live-set public origin.
    """
    if explicit is not None:
        raw = explicit
    else:
        raw = ""
        if settings is not None:
            raw = getattr(settings, "preview_public_base", None) or ""
        if not str(raw or "").strip():
            raw = os.environ.get("BOSS_PREVIEW_PUBLIC_BASE", "") or ""
    base = (raw or "").strip().rstrip("/")
    if not base:
        return ""
    if _is_loopback_url(base):
        return ""
    return base


def operator_preview_open_url(
    preview_id: str,
    *,
    public_base: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> str:
    """URL a browser can open for the preview (reverse-proxied by the BOSS API).

    The preview process binds 127.0.0.1 inside the API host; only this gateway path is
    reachable from an operator browser or agent on the tailnet. When
    ``BOSS_PREVIEW_PUBLIC_BASE`` (or ``public_base``) is set to a non-loopback origin,
    returns an absolute URL; otherwise a relative gateway path.
    """
    path = f"/v1/previews/{preview_id}/web/"
    base = _preview_public_base(public_base, settings=settings)
    if base:
        return f"{base}{path}"
    return path


def publicize_preview(
    record: dict,
    *,
    public_base: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> dict:
    """Never hand out the preview's internal address as the primary ``url``.

    ``app_previews.url`` holds the sandbox-internal address
    (``http://127.0.0.1:43053``), reachable from the API process and from nowhere a
    browser runs. Listing that column as ``url`` is how operators and agents were
    told to open an unreachable address.

    The internal address is preserved under ``internal_url`` for diagnostics and
    for the in-process reverse proxy, which still probes the loopback port.

    When ``BOSS_PREVIEW_PUBLIC_BASE`` is set, operator-facing ``url`` / ``open_url``
    become absolute under that origin. A loopback public base is rejected.
    """
    out = dict(record)
    preview_id = out.get("id")
    internal = out.get("url")
    if internal is not None:
        out["internal_url"] = internal
    if not preview_id:
        # Still strip loopback from primary url if someone handed us a bare record.
        if _is_loopback_url(out.get("url")):
            out["url"] = None
        return out
    # Any record that ever had a live bind (or is still marked running) gets a
    # gateway open URL as the primary `url`. Failed/never-started rows with no
    # bind stay url=null so the console does not offer a dead "Open" link.
    if internal is not None or str(out.get("status") or "").lower() == "running":
        open_url = operator_preview_open_url(
            str(preview_id), public_base=public_base, settings=settings
        )
        out["open_url"] = open_url
        out["url"] = open_url
    # Hard invariant: primary operator URL must never be loopback, even if a
    # non-gateway absolute slipped into the DB column without publicize rewrite.
    if _is_loopback_url(out.get("url")):
        open_url = operator_preview_open_url(
            str(preview_id), public_base=public_base, settings=settings
        )
        out["open_url"] = open_url
        out["url"] = open_url
    return out


FIXTURE_MODE = "fixture"

FIXTURE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>BOSS preview fixture</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 36rem; margin: 3rem auto; padding: 0 1rem; }
    .badge { display: inline-block; border: 1px solid #888; border-radius: 999px; padding: 2px 10px; font-size: 12px; }
  </style>
</head>
<body>
  <p class="badge">fixture — not a sandboxed app</p>
  <h1>Preview View path is live</h1>
  <p>This page is served by the BOSS API gateway at <code>/v1/previews/{preview_id}/web/</code>.
     It is a no-spend fixture so operators can prove View HTML without a microVM rootfs.</p>
  <p data-testid="preview-fixture">ok</p>
</body>
</html>
"""


def create_fixture_preview(
    engine,
    settings: Settings,
    *,
    project_id: str,
    actor: str,
    ttl_minutes: int = 60,
) -> dict:
    """No-spend running preview that serves fixture HTML through the gateway.

    Does not clone a repo, start a process, or claim a microVM. Status is
    ``running`` with ``mode=fixture`` so View HTML can be proven while sandbox
    claims stay frozen. Never advertised as a real app preview.
    """
    preview_id = make_id("prev")
    ttl = max(5, min(int(ttl_minutes), 180))
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl)
    with begin(engine) as connection:
        connection.execute(
            text(
                """
                INSERT INTO app_previews (
                    id, project_id, run_id, repo, branch, command, port, url,
                    mode, substrate, status, error, expires_at
                )
                VALUES (
                    :id, :project_id, NULL, NULL, NULL, 'fixture', NULL, NULL,
                    :mode, 'preview_host', 'running',
                    'fixture — not a sandboxed app', :expires
                )
                """
            ),
            {
                "id": preview_id,
                "project_id": project_id,
                "mode": FIXTURE_MODE,
                "expires": expires,
            },
        )
        emit_event(
            connection,
            project_id,
            "preview.fixture_started",
            actor,
            "app_preview",
            preview_id,
            {"mode": FIXTURE_MODE, "ttl_minutes": ttl, "simulated": True},
        )
        row = connection.execute(
            text("SELECT * FROM app_previews WHERE id = :id"),
            {"id": preview_id},
        ).mappings().first()
    record = publicize_preview(dict(row), settings=settings)
    record["fixture"] = True
    record["simulated"] = True
    return record


def fixture_html(preview_id: str) -> bytes:
    return FIXTURE_HTML.replace("{preview_id}", preview_id).encode("utf-8")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _low_priority(command: list[str]) -> list[str]:
    """Run a preview build at LOW CPU/IO priority.

    The diagnosis this docstring used to carry — `npm install` + `vite build` run inside the api
    container and at normal priority starve it into returning 404s ("Reconnecting to the control
    plane") — was RIGHT, and was applied to preview builds only. The code agent and the sandbox
    do the same work for far longer and were never given the same treatment.

    So the rule moved to services/scheduling.py and all three call it. This wrapper stays because
    it is the name the preview paths already use.
    """
    return low_priority(command)


# Single source of truth for "which python file starts this app". Kept as one constant because
# the host path (_detect_command) and the container path (_generate_dockerfile) MUST agree --
# they previously carried separate copies of this tuple, and an app whose entrypoint was named
# serve.py failed to launch in both while the test fallback reported a misleading 'verified'.
_PY_ENTRYPOINTS = ("server.py", "serve.py", "app.py", "wsgi.py", "main.py")
# Directories a static build drops its index.html into, in priority order ("." = repo root).
_STATIC_DIRS = (".", "web", "public", "dist", "site", "static")
# Basename prefixes that SUGGEST a Python entrypoint. A suggestion only: see
# python_source_is_program for why a name is never enough on its own.
_PY_ENTRY_PREFIXES = ("serve", "server", "app", "main", "wsgi")

# Calls that construct or start an HTTP server/app object. Matched on the called name's last
# component, so `http.server.ThreadingHTTPServer(...)` and `ThreadingHTTPServer(...)` both count.
_PY_SERVER_CALLS = frozenset({
    "Flask", "FastAPI", "Starlette", "Quart", "Sanic", "Bottle", "Application",
    "HTTPServer", "ThreadingHTTPServer", "TCPServer", "ThreadingTCPServer",
    "make_server", "serve_forever", "run_app", "run_simple",
})


def _called_name(node) -> str:
    import ast
    func = node.func if isinstance(node, ast.Call) else None
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def python_source_is_program(source: str) -> bool:
    """True when running this file as a script would DO something, not merely define things.

    WHY. lead_triage_core/approvals.py was previewed as the Lead Triage Inbox app (boss-1,
    crun_3e370c114b95425d8a7c, PR paramg/boss-sandbox#218) because its name starts with "app".
    It is a library module: imports, dataclasses, functions — and relative imports, so running
    it as `python3 lead_triage_core/approvals.py` died at once with "ImportError: attempted
    relative import with no known parent package". The preview waited for a port that nothing
    would ever open and reported a timeout, twice.

    A name says what a file is CALLED. Whether it is a program is in its body: an
    `if __name__ == "__main__":` block, a top-level call that runs something (`app.run()`,
    `serve()`, `print(...)`), or construction of a known server/app object (`Flask(__name__)`,
    `ThreadingHTTPServer(...)`). A module of definitions is none of those, whatever its name.

    A file that does not parse is judged by the `__main__` guard alone, textually — a syntax
    error is the build's problem to report, not a reason to stop recognising its entrypoint.
    """
    import ast
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError):
        return bool(re.search(r"""__name__\s*==\s*['"]__main__['"]""", source or ""))
    for node in tree.body:
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"):
                return True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            return True
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
                getattr(node, "value", None), ast.Call):
            if _called_name(node.value) in _PY_SERVER_CALLS:
                return True
    return False


def python_source_serves(source: str) -> bool:
    """True when the file constructs or starts an HTTP server/app object anywhere in it."""
    import ast
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError):
        return False
    return any(isinstance(n, ast.Call) and _called_name(n) in _PY_SERVER_CALLS
               for n in ast.walk(tree))


def _read_py(workdir: Path, rel: str) -> Optional[str]:
    try:
        return (workdir / rel).read_text(errors="ignore")
    except OSError:
        return None


def _is_py_test_path(rel: str) -> bool:
    p = "/" + rel.replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    return (any(seg in p for seg in ("/tests/", "/test/", "/e2e/"))
            or base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py")


def python_entry_invocation(workdir: Path, rel: str) -> tuple[str, list[str]]:
    """(directory to run from, argv) for ONE Python entry file. The only place that decides it.

    A module inside a package (its directory holds __init__.py) is run as
    `python3 -m pkg.module` from the directory ABOVE the outermost package, which is what makes
    its relative imports resolve. `python3 pkg/module.py` puts pkg/ itself on sys.path with no
    parent package, and any `from .x import y` in it dies with "attempted relative import with
    no known parent package" — the error the Lead Triage Inbox preview hit (crun_3e370c11).
    A file that is not in a package runs as a plain script, exactly as before.
    """
    rel = rel.strip()
    if rel.startswith("./"):
        rel = rel[2:]
    path = Path(rel)
    parts = list(path.parent.parts)
    pkg_start = len(parts)
    while pkg_start > 0 and (workdir.joinpath(*parts[:pkg_start]) / "__init__.py").is_file():
        pkg_start -= 1
    if pkg_start == len(parts):
        return ".", ["python3", rel]
    module = ".".join(parts[pkg_start:] + [path.stem])
    run_dir = str(Path(*parts[:pkg_start])) if pkg_start else "."
    return run_dir, ["python3", "-m", module]



# The file a build writes to say WHICH app it produced, as "<kind>:<value>" (e.g.
# "py:serve_scorecard.py", "node:bugbox", "static:web-nqueens"). See declare_app_entry.
DECLARED_APP_FILE = "boss.preview.app"


def declare_app_entry(paths, read=None) -> Optional[str]:
    """The app entry a build produced, derived ONLY from the files that build wrote.

    WHY THIS EXISTS — the fix for a bug fixed five times.

    Every previous fix taught resolve_app_entry one more repo shape, and the next shape broke
    it again: a task that added serve_nqueens.py previewed SUDOKU (PR #62); a task that wrote
    bugbox/ previewed the repo root (PR #161); a Python task previewed a vite root that had no
    entry module. The constant is not the shapes, it is that the preview RE-DERIVES a fact the
    build already knew, from a repository that has accumulated one more app on every run — nine
    runnable apps by the time the interview-scorecard build ran, ten by the next.

    The signal is strongest at build time and decays afterwards. So the decision is made once,
    here, against this run's OWN files, and committed to the branch. Later previews read it
    instead of guessing, and no amount of accumulation in the repository can mislead them.

    Returns None when this run's files do not clearly identify one app — a build that touched
    only tests or docs, or one that somehow produced two servers. None means "say nothing and
    let the existing resolver try", never "guess".
    """
    # Contents known means programs can be told from modules (python_source_is_program); a
    # caller with only paths gets the older name-only rule rather than nothing.
    contents_known = read is not None
    read = read or (lambda _p: "")
    owned = [str(p).strip().lstrip("./") for p in (paths or []) if str(p).strip()]

    def _dir_of(path: str) -> str:
        head, _, _tail = path.rpartition("/")
        return head or "."

    # A server this run wrote outranks static assets the same run wrote: serving an app's
    # index.html without its API is a page that loads and does nothing.
    node_dirs, py_entries, static_dirs = [], [], []
    for path in owned:
        name = path.rpartition("/")[2]
        if name == "package.json":
            try:
                scripts = (json.loads(read(path) or "{}") or {}).get("scripts") or {}
            except (ValueError, TypeError):
                scripts = {}
            if str(scripts.get("start") or "").strip():
                node_dirs.append(_dir_of(path))
        elif name.endswith(".py") and contents_known:
            # WITH THE SOURCE IN HAND, A NAME IS NOT THE TEST. A module that only defines things
            # is never the app (approvals.py, crun_3e370c11), and a server that lives inside its
            # package — lead_triage_core/server.py — is, even though it is not at the root.
            # A non-root file must both be a program and construct a server: that is what keeps
            # `myapp/server_helpers.py` from being declared on a prefix match.
            if _is_py_test_path(path):
                continue
            source = read(path) or ""
            if not python_source_is_program(source):
                continue
            if "/" not in path and (name in _PY_ENTRYPOINTS or name.startswith(("serve", "server"))):
                py_entries.append(path)
            elif python_source_serves(source):
                py_entries.append(path)
        elif "/" not in path and name.endswith(".py") and (
            name in _PY_ENTRYPOINTS or name.startswith(("serve", "server"))
        ):
            # ROOT LEVEL ONLY, matching resolve_app_entry's own scope. Without this, an ordinary
            # module like `myapp/server_helpers.py` reads as an entrypoint on a prefix match and
            # gets declared as the app — a wrong declaration is worse than none, because every
            # later preview trusts it instead of falling back to detection.
            py_entries.append(path)
        elif name == "index.html":
            static_dirs.append(_dir_of(path))

    for kind, found in (("node", node_dirs), ("py", py_entries), ("static", static_dirs)):
        unique = sorted(set(found))
        if not unique:
            continue
        # Two servers from one build is not a thing this can name. Stay silent rather than
        # commit a declaration that sends every future preview to the wrong one.
        if len(unique) > 1:
            return None
        return f"{kind}:{unique[0]}"
    return None


# The file in which a BUILD states how to run what it built: one shell command that starts
# the app and serves HTTP on $PORT. Equivalent: `"boss": {"preview": {"command": ...}}` in a
# package.json. The command runs from the directory holding the declaration.
RUN_DECLARATION_FILE = "boss.preview.command"


def _declared_command_in(directory: Path) -> Optional[str]:
    """The run command declared in ONE directory, from either form, or None."""
    hint = directory / RUN_DECLARATION_FILE
    if hint.is_file():
        command = hint.read_text(errors="ignore").strip()
        if command:
            return command
    package = directory / "package.json"
    if package.is_file():
        try:
            payload = json.loads(package.read_text()) or {}
        except (OSError, ValueError):
            payload = {}
        configured = ((payload.get("boss") or {}).get("preview") or {}).get("command") \
            if isinstance(payload, dict) else None
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
    return None


def find_run_declaration(workdir: Path, changed: Optional[list[str]] = None
                         ) -> Optional[tuple[str, str]]:
    """(directory, command) the build DECLARED for running its app, or None.

    WHY THIS OUTRANKS EVERY RULE IN resolve_app_entry. That function has been patched once per
    incident — Sudoku for N-Queens (PR #62), the repo root for BugBox, a vite root with no
    entry, and lead_triage_core/approvals.py for the Lead Triage Inbox (crun_3e370c11) — because
    it infers at preview time a fact only the build knows. The implement stage is now required
    to write this declaration, and the build proves it serves (prove_app_serves) before the run
    can pass, so the preview runs a command that is already known to work.

    Only a declaration THIS lineage wrote counts when the lineage is known: a shared repo
    accumulates apps, and a declaration another app left at the root must not hijack this one.
    With no lineage at all (changed is None or empty) a root declaration is honoured, as it
    always was.
    """
    owned = [str(c).strip() for c in (changed or []) if str(c).strip()]
    for rel in owned:
        name = rel.rsplit("/", 1)[-1]
        if name not in (RUN_DECLARATION_FILE, "package.json"):
            continue
        rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else "."
        directory = workdir if rel_dir == "." else workdir / rel_dir
        command = _declared_command_in(directory)
        if command:
            return rel_dir, command
    if not owned:
        command = _declared_command_in(workdir)
        if command:
            return ".", command
    return None


def run_declared_command(rel_dir: str, command: str) -> list[str]:
    """The argv for a declared command, run from the directory that declared it."""
    if rel_dir in ("", "."):
        return ["sh", "-lc", command]
    return ["sh", "-lc", f"cd {shlex.quote(rel_dir)} && {command}"]


def parse_declared_app(text: str) -> Optional[tuple[str, str]]:
    """Read back what declare_app_entry wrote. Unknown kinds are ignored, not trusted."""
    kind, sep, value = (text or "").strip().partition(":")
    if not sep or kind not in {"node", "py", "static"} or not value.strip():
        return None
    return kind, value.strip()


class AmbiguousAppError(RuntimeError):
    """The repo holds several runnable apps and this task gave no signal which one is its own.

    Raised rather than guessed. The previous behaviour returned the first convention match,
    which showed an operator an application their task never touched — and in a repo shared
    across customers, another tenant's app entirely.
    """

    def __init__(self, candidates: list[str]) -> None:
        self.candidates = candidates
        super().__init__(
            "cannot tell which app this task built: the repository contains "
            f"{len(candidates)} runnable apps ({', '.join(candidates)}) and the run recorded "
            "no implementation files identifying one. Refusing to preview an arbitrary app. "
            "Retry the task so it records what it built, or add a boss.preview.command file."
        )


# The branch a run was built ON is the app it changes.
_WORK_BRANCH = re.compile(r"^boss/code/(crun_[a-z0-9]+)$", re.IGNORECASE)


def app_root_run_id(engine, run: dict) -> Optional[str]:
    """The run that ESTABLISHED this app, following every link BOSS records.

    Lineage was read from root_run_id and deferred_from_run only. Work started as its own task
    on an existing app's branch -- which is what "change this app" and every follow-up build
    does -- records neither: it records `spec.base_branch = boss/code/<run>`. So its file list
    was just the files that attempt happened to touch.

    For "Official bugbox - honest verify + JTBD e2e" that was the verification and e2e files.
    No index.html among them, so resolve_app_entry fell through to repo conventions and picked
    the repo ROOT, whose package.json has a `preview` script and no app to preview: rollup had
    no entry module to load and the preview timed out. The app it should have served --
    bugbox/, with its own index.html and server -- was never considered.

    The branch is the link. Walking it makes every attempt at an app resolve to the same app,
    for every stack, which is the property the three earlier fixes to this file were each
    reaching for one case at a time.
    """
    seen: set[str] = set()
    current = run
    for _ in range(10):                       # a cycle here would hang the preview
        fctx = current.get("finalize_ctx") or {}
        run_id = current.get("id")
        if run_id in seen:
            break
        if run_id:
            seen.add(run_id)
        root = fctx.get("root_run_id")
        if root and root != run_id:
            nxt = root
        else:
            spec = current.get("spec") or {}
            base = spec.get("base_branch") or ""
            matched = _WORK_BRANCH.match(str(base))
            nxt = matched.group(1) if matched else None
            if nxt == run_id:
                nxt = None
        if not nxt:
            return run_id
        with begin(engine) as c:
            row = c.execute(
                text("SELECT id, spec, finalize_ctx FROM code_task_runs WHERE id=:i"),
                {"i": nxt},
            ).mappings().first()
        if not row:
            # The branch names a run that no longer exists; this run is as far back as we go.
            return run_id
        current = dict(row)
    return current.get("id")


def lineage_impl_files(engine, run: dict) -> list[str]:
    """Every implementation file THIS TASK'S LINEAGE has written, newest attempt first.

    `state.impl_files` is per-ATTEMPT. On a repair it holds only the touched-up files — for
    crun_9b8e732b that was ["README.md", "nqueens_core/levels.py"], with no trace of the
    web-nqueens/ app the task exists to build. resolve_app_entry then finds no index.html
    among them and falls back to the repo-wide serve.py, so the preview served SUDOKU for an
    N-Queens task. Again.

    The branch holds the whole lineage's work, so the file list must too. (Deriving it with
    `git diff base...HEAD` is not an option: the preview clones --depth 1 --branch X, so the
    base ref does not exist in that workdir.)
    """
    fctx = run.get("finalize_ctx") or {}
    root = fctx.get("root_run_id")
    if not root and fctx.get("deferred_from_run"):
        # A deferred-findings FOLLOW-UP is spawned as its own root, so its file list is only the
        # findings it fixed — for crun_022620a9 that was ["nqueens_core/levels.py"], with no app
        # in it at all. It builds on the SAME branch as the run it came from, so its preview must
        # inherit that run's lineage. Resolved here rather than by writing root_run_id on the
        # follow-up, which would make the pipeline treat it as a retry and change its stages.
        with begin(engine) as c:
            parent = c.execute(
                text("SELECT COALESCE(finalize_ctx->>'root_run_id', id) AS r "
                     "FROM code_task_runs WHERE id=:i"),
                {"i": fctx["deferred_from_run"]},
            ).mappings().first()
        if parent:
            root = parent["r"]
    # Follow base_branch as well, so work started as its own task on an existing app's branch
    # inherits that app's files instead of only what this attempt touched.
    root = root or app_root_run_id(engine, run) or run.get("id")
    if not root:
        return list((run.get("state") or {}).get("impl_files") or [])
    with begin(engine) as c:
        rows = c.execute(
            text("""
                SELECT state->'impl_files' AS f
                FROM code_task_runs
                WHERE COALESCE(finalize_ctx->>'root_run_id', id) = :root
                   OR finalize_ctx->>'deferred_from_run' IN (
                        SELECT id FROM code_task_runs
                        WHERE COALESCE(finalize_ctx->>'root_run_id', id) = :root)
                ORDER BY created_at DESC
            """),
            {"root": root},
        ).mappings().all()
    out: list[str] = []
    for r in rows:
        for path in (r["f"] or []):
            if isinstance(path, str) and path not in out:
                out.append(path)
    return out


def _changed_dirs(changed: Optional[list[str]]) -> list[str]:
    """Directories this task touched, in the order they appear in the diff."""
    out: list[str] = []
    for c in changed or []:
        d = c.rsplit("/", 1)[0] if "/" in c else "."
        if d not in out:
            out.append(d)
    return out


# Conventional frontend folders of a full-stack app. Walking UP through these to find
# package.json `start` is correct (bugbox/web → bugbox). Walking from a sibling product
# directory (web-nqueens) up to an unrelated root start is how we once previewed the
# wrong tenant's application — so those stop before the repo root.
_FRONTEND_DIR_NAMES = frozenset({
    "web", "public", "dist", "src", "frontend", "client", "app", "ui", "static", "www",
})


def _has_start_script(directory: Path) -> bool:
    """True when directory/package.json declares a non-empty scripts.start."""
    manifest = directory / "package.json"
    if not manifest.is_file():
        return False
    try:
        scripts = json.loads(manifest.read_text()).get("scripts") or {}
    except (json.JSONDecodeError, OSError):
        return False
    return bool(str(scripts.get("start") or "").strip())


def _owning_node_server(workdir: Path, rel_dir: str) -> Optional[str]:
    """Nearest ancestor of rel_dir (inclusive) that declares scripts.start, or None.

    Used so a task that touched `bugbox/web/index.html` still runs `bugbox`'s Express
    server instead of `python3 -m http.server` on the SPA. A sibling static app
    (`web-nqueens/`) is not treated as owned by a root start.
    """
    parts: list[str] = [] if rel_dir in (".", "") else list(Path(rel_dir).parts)
    while True:
        candidate = workdir.joinpath(*parts) if parts else workdir
        if _has_start_script(candidate):
            return "." if not parts else str(Path(*parts))
        if not parts:
            return None
        if len(parts) == 1 and parts[0] not in _FRONTEND_DIR_NAMES:
            return None
        parts.pop()


def _node_server_dir(workdir: Path, changed: Optional[list[str]] = None) -> Optional[str]:
    """The directory of a package.json that declares a real `start` server, or None.

    A `start` script is the signal that something must be RUN, not served. Without this the
    resolver reached its static branch first -- and a Vite app has an `index.html` at its root,
    so an app with a working Express backend was classified `static:.` and published as raw
    source files. In the ApproveIt build the frontend loaded and every /api call 404'd, and the
    operator reasonably concluded the agent had built a shell.

    Subdirectories are checked before the root, and directories the task actually touched
    (including ancestors of `web/` / `public/`) before those it did not, so a repo holding
    several apps runs the one this task built.

    `dev`/`build` alone are deliberately NOT enough: a frontend-only Vite app has those and is
    correctly deployed as static output. Only `start` means "there is a server here".
    """
    seen: list[str] = []
    for touched in _changed_dirs(changed):
        owner = _owning_node_server(workdir, touched)
        if owner is not None and owner not in seen:
            seen.append(owner)
    for manifest in sorted(workdir.glob("*/package.json")):
        if _has_start_script(manifest.parent):
            rel = manifest.parent.name
            if rel not in seen:
                seen.append(rel)
    if _has_start_script(workdir) and "." not in seen:
        seen.append(".")
    # One unambiguous server wins. Several means the same ambiguity the static path refuses,
    # and guessing is what once previewed another tenant's application.
    return seen[0] if seen else None


def resolve_app_entry(workdir: Path, changed: Optional[list[str]] = None) -> tuple[str, str]:
    """THE single decision about which app a preview launches: ("py"|"static"|"node"|"", value).

    Both the host path (_detect_command) and the container path (_generate_dockerfile) call
    THIS. They previously each carried their own copy of the preference logic, and the copies
    drifted twice:

      PR #62  — the host path learned to prefer the entrypoint the task built; the container
                path did not, and previewed Sudoku for an N-Queens build.
      crun_9b8e732b — both paths then preferred a CHANGED .py entrypoint, but neither applied
                the same preference to STATIC dirs. _STATIC_DIRS tries "." first, and a task
                that built `web-nqueens/index.html` in a repo whose root already held the
                Sudoku app previewed Sudoku again — this time via the repo-wide serve.py.

    Hence one function. "What this task built" outranks "what the repo happens to contain",
    for every stack, and there is exactly one place to change that rule.
    """
    touched = _changed_dirs(changed)

    # 1. An entrypoint THIS task wrote — a PROGRAM, not a name.
    #
    # This rule took any changed .py whose basename began with serve/server/app/main/wsgi, so
    # lead_triage_core/approvals.py ("app…") was chosen over the server the same build wrote,
    # run as a script, and died on its own relative imports (crun_3e370c11). A candidate must
    # now be a program (python_source_is_program). A name-matched program wins first, as
    # before; a program that constructs a server is the app whatever it is called. Package
    # modules are run with -m by command_for_entry, so choosing one here is safe.
    named, serving = [], []
    for cand in [c for c in (changed or []) if c.endswith(".py") and not _is_py_test_path(c)]:
        source = _read_py(workdir, cand)
        if source is None or not python_source_is_program(source):
            continue
        if cand.rsplit("/", 1)[-1].startswith(_PY_ENTRY_PREFIXES):
            named.append(cand)
        elif python_source_serves(source):
            serving.append(cand)
    if named or serving:
        return ("py", (named or serving)[0])
    # 2. A static app THIS task built. Ranked ABOVE the repo-wide python fallback: a task that
    #    ships web-nqueens/index.html means that app, not whatever serve.py predates it.
    #
    #    A start server that OWNS that index.html outranks the static file. BugBox/ApproveIt
    #    lineage lists `bugbox/web/index.html`; returning static here previewed the SPA and
    #    every /api call came back as the Vite 404 HTML page. The owning check walks ancestors
    #    (bugbox/web → bugbox) and refuses to let a root start steal a sibling product dir.
    for d in touched:
        if (workdir / d / "index.html").is_file():
            owner = _owning_node_server(workdir, d)
            if owner is not None:
                return ("node", owner)
            return ("static", d)
    # 3. Repo conventions — but ONLY when the repo is unambiguous.
    #
    # FAIL CLOSED ON AMBIGUITY. This used to walk the conventions and return the first hit, so
    # a task whose own output revealed no app previewed whatever the repo happened to contain.
    # A deferred-findings follow-up touching only nqueens_core/levels.py showed the operator
    # the SUDOKU app sitting at the repo root. In a repo holding one customer's work that is a
    # confusing preview; in a repo holding several it is showing someone another tenant's
    # application. Serving the wrong app is strictly worse than serving nothing, so when the
    # repo offers more than one candidate and the task gives no signal, we refuse and say why.
    # A CANDIDATE MUST BE ABLE TO RUN.
    #
    # boss-sandbox's ROOT package.json carries `"preview": "vite preview"` and the root holds
    # no index.html and no src/ -- it is a monorepo root, not an app. Anything that selected it
    # produced `rollup ... loadEntryModule` and a preview that never served, reported to the
    # operator as "app did not serve HTTP within timeout": a timeout for a build that could
    # never have succeeded. A directory whose node scripts have nothing to build is not a
    # candidate, whatever else the conventions say about it.
    def _node_dir_can_build(rel: str) -> bool:
        base = workdir / rel if rel != "." else workdir
        manifest = base / "package.json"
        if not manifest.is_file():
            return False
        try:
            scripts = (json.loads(manifest.read_text()) or {}).get("scripts") or {}
        except (ValueError, OSError):
            return False
        # A server entry can serve without an index.html; a bundler cannot.
        if scripts.get("start"):
            return True
        if not any(k in scripts for k in ("dev", "build", "preview")):
            return False
        return (base / "index.html").is_file()

    candidates: list[tuple[str, str]] = []

    def _root_program(name: str) -> bool:
        # Same rule as step 1: a module of definitions named app_utils.py is not an app, and
        # counting it made a repo with one real server look ambiguous (or chose the module).
        source = _read_py(workdir, name)
        return source is not None and python_source_is_program(source)

    for entry in _PY_ENTRYPOINTS:
        if (workdir / entry).exists() and _root_program(entry):
            candidates.append(("py", entry))
    for cand in sorted(workdir.glob("*.py")):
        if cand.name.startswith(_PY_ENTRY_PREFIXES) and _root_program(cand.name):
            if ("py", cand.name) not in candidates:
                candidates.append(("py", cand.name))
    # A runnable server outranks its own static assets: serving an app's index.html without
    # its API is a page that loads and does nothing.
    server_dir = _node_server_dir(workdir, changed)
    if server_dir is not None and _node_dir_can_build(server_dir):
        return ("node", server_dir)
    for rel in _STATIC_DIRS:
        if (workdir / rel / "index.html").is_file():
            candidates.append(("static", rel))
    # Any OTHER top-level directory holding an index.html is a candidate too. Counting only the
    # convention names made a multi-app repo look unambiguous: boss-sandbox holds web/ (Sudoku),
    # web-stock/ and web-nqueens/, but only "web" is a convention, so a task with no signal got
    # a confident answer of SUDOKU rather than the refusal this function promises. Ambiguity has
    # to be measured against what the repo actually contains.
    for cand in sorted(workdir.glob("*/index.html")):
        rel = cand.parent.name
        if ("static", rel) not in candidates:
            candidates.append(("static", rel))
    # Last-resort root node app -- but only if the root can actually build. A monorepo root
    # whose package.json exists purely to hold a `preview` script is not an app, and choosing
    # it produced a preview that could never serve.
    if not candidates and (workdir / "package.json").exists() and _node_dir_can_build("."):
        candidates.append(("node", "."))
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise AmbiguousAppError([f"{k}:{v}" for k, v in candidates])
    return ("", "")


def _detect_command(workdir: Path, changed: Optional[list[str]] = None) -> Optional[list[str]]:
    """Build the run command FOR THE APP resolve_app_entry chose.

    THE BUG THIS FIXES. resolve_app_entry is the single decision about which app a preview
    launches, and this function ignored it: it read `workdir/package.json` -- the repo ROOT --
    and built a command from that. In boss-sandbox the root is a monorepo root whose
    devDependencies contain `vite`, so the Vite branch fired and ran `npx vite build` at a
    directory with no index.html and no src. Rollup had nothing to load
    ("ModuleLoader.loadEntryModule"), nothing ever listened, and the operator was told "app did
    not serve HTTP within timeout" -- three times, while resolution was correct all along and
    answered `node:bugbox` every time.

    Resolution being right is worthless if the command ignores it. So the resolved directory is
    where the command is built AND where it runs.
    """
    # HOW THE BUILD SAID TO RUN IT beats everything, including which app BOSS derived: the
    # build wrote the command and proved it served before it passed (find_run_declaration).
    run_declaration = find_run_declaration(workdir, changed)
    if run_declaration:
        logger.info("preview: using the run command declared by the build in %s",
                    run_declaration[0])
        return run_declared_command(*run_declaration)

    # WHAT THE BUILD DECLARED BEATS ANYTHING INFERRED LATER. The build knew which app it
    # produced; everything below is this function trying to work that out again from a
    # repository that has gained apps since. Read the declaration first and skip the guessing.
    declared = workdir / DECLARED_APP_FILE
    if declared.is_file():
        entry = parse_declared_app(declared.read_text(errors="ignore"))
        if entry:
            command = command_for_entry(workdir, entry[0], entry[1])
            if command:
                logger.info("preview: using the app declared by the build: %s:%s", *entry)
                return command
            logger.warning(
                "preview: build declared %s:%s but no command could be built for it — "
                "falling back to detection", *entry)

    app_rel = "."
    try:
        kind, value = resolve_app_entry(workdir, changed)
        # A RESOLVED PYTHON APP IS THE ANSWER, NOT A REASON TO GO READ THE ROOT MANIFEST.
        #
        # Only node/static were honoured here, so a task whose app is a Python service fell
        # through to _detect_command_in(root) — and in a repo whose root package.json declares
        # vite, the Vite branch fired before anything asked about Python. The interview-scorecard
        # run built scorecard_core/ + serve_scorecard.py, resolution answered
        # `py:serve_scorecard.py` correctly, and the preview ran `npx vite build` at the
        # workspaces root: "Build failed in 6ms | Could not resolve entry module index.html",
        # nothing listened, "app did not serve HTTP within timeout" — for an app that was never
        # started. The whole point of this function is that resolution being right is worthless
        # if the command ignores it; py was the case still ignoring it.
        if kind == "py" and value:
            return command_for_entry(workdir, kind, value)
        if kind in {"node", "static"} and value not in {"", "."}:
            app_rel = value
    except AmbiguousAppError:
        # REFUSE, DO NOT FALL BACK TO THE ROOT. Swallowing this turned a precise, actionable
        # refusal ("the repository contains 9 runnable apps and the run recorded no
        # implementation files identifying one") into a silent guess at the repo root — and the
        # root is exactly the directory resolution had just declined to choose. The callers
        # already handle this: it is recorded as failure_kind 'ambiguous_app' with the message
        # above, which tells the operator what to do. Fail closed and say why.
        raise
    command = _detect_command_in(workdir if app_rel == "." else workdir / app_rel, changed)
    if command and app_rel != "." and command[:2] == ["sh", "-lc"]:
        # Run it where the app is. `npm install` in the wrong directory installs the wrong
        # dependency tree, which is the other half of why this failed the way it did.
        return ["sh", "-lc", f"cd {shlex.quote(app_rel)} && {command[2]}"]
    return command


def _detect_command_in(workdir: Path, changed: Optional[list[str]] = None) -> Optional[list[str]]:
    """Detect the declared preview command for ONE app directory."""
    configured = _declared_command_in(workdir)
    if configured:
        return ["sh", "-lc", configured]
    package = workdir / "package.json"
    if package.is_file():
        try:
            payload = json.loads(package.read_text())
        except (OSError, json.JSONDecodeError):
            payload = {}
        scripts = payload.get("scripts") or {}
        deps = {**(payload.get("dependencies") or {}), **(payload.get("devDependencies") or {})}
        is_vite = ("vite" in deps or (workdir / "vite.config.ts").exists()
                   or (workdir / "vite.config.js").exists())
        # A DEPENDENCY IS NOT AN APPLICATION.
        #
        # `vite` in package.json says the toolchain is available, not that anything was built
        # with it. A task seeded with the full-stack scaffold and then written as a Python
        # service ships exactly that: the scaffold's package.json, and no index.html anywhere.
        # `vite build` then fails in 11ms with "Could not resolve entry module index.html",
        # the preview times out waiting for a port, and the operator is told to retry a task
        # that completed perfectly well and opened PR #161.
        #
        # Vite cannot build without an entry, so if there is no index.html to be found this is
        # not a Vite app whatever the manifest claims — fall through to the other detectors,
        # which can find the Python or Node server that IS there.
        if is_vite and not any((workdir / d / "index.html").is_file() for d in _STATIC_DIRS):
            logger.info(
                "preview: package.json declares vite but no index.html exists under %s — "
                "not treating this as a Vite app",
                ", ".join(_STATIC_DIRS),
            )
            is_vite = False
        # A Vite app: BUILD it and serve the STATIC output — do NOT run the dev server. The dev
        # server emits origin-absolute ES-module imports (/node_modules/.vite/deps, /src, /@vite)
        # that the browser resolves against the API origin root, NOT the /v1/previews/<id>/web
        # proxy prefix, so every module 404s and the page renders blank. A production build bundles
        # to RELATIVE-path assets (base=./) that resolve correctly under any proxy prefix via the
        # injected <base href>. Served by Python's stdlib http.server (guaranteed present).
        if is_vite and (scripts.get("build") or "vite" in deps):
            # A Vite app WITH a server of its own: build the frontend, then run that server.
            #
            # This branch used to fire on `vite` alone and serve dist/ with http.server, which
            # is right for a frontend-only app and silently wrong for a full-stack one: the
            # page loads, looks finished, and every /api call returns 404. BugBox shipped a
            # working Express + SQLite backend and previewed as "Error: HTTP 404" because
            # nothing was running it.
            #
            # The build still uses --base=./ rather than the app's own build script. Assets
            # must be RELATIVE to resolve under the /v1/previews/<id>/web prefix; an absolute
            # /assets/... path resolves against the API origin and 404s, and the injected
            # <base href> cannot rescue it.
            if scripts.get("start"):
                return [
                    "sh", "-lc",
                    "npm install --no-audit --no-fund --loglevel=error && "
                    "npx vite build --base=./ && "
                    "npm start",
                ]
            return [
                "sh", "-lc",
                "npm install --no-audit --no-fund --loglevel=error && "
                "npx vite build --base=./ && "
                'python3 -m http.server "$PORT" --bind 0.0.0.0 --directory dist',
            ]
        script = "dev" if scripts.get("dev") else "start" if scripts.get("start") else None
        if script:
            suffix = " -- --host 0.0.0.0" if script == "dev" else ""
            # `start` RUNS the app; it does not compile it. A TypeScript service whose start
            # script is `node dist/server.js` needs `dist/` to exist first, and this branch went
            # straight from install to start — so the app died on MODULE_NOT_FOUND and the
            # operator watched "Starting preview…" for minutes before it timed out. Shorty, a
            # complete and correct URL shortener, could not be previewed for exactly this reason.
            #
            # The build was already run on the two Vite branches above and in the Dockerfile
            # path; this branch and no other was missed, which is the whole shape — the same
            # requirement expressed in four places and satisfied in three.
            #
            # `dev` is excluded deliberately: a dev server compiles on the fly, and building
            # first would only slow it down. `--if-present` makes this a no-op for an app that
            # has no build script, so nothing that worked before changes.
            build = "" if script == "dev" else "npm run build --if-present && "
            return [
                "sh", "-lc",
                "npm install --no-audit --no-fund --loglevel=error && "
                f"{build}npm run {script}{suffix}",
            ]
    procfile = workdir / "Procfile"
    if procfile.is_file():
        for line in procfile.read_text(errors="ignore").splitlines():
            kind, sep, command = line.partition(":")
            if sep and kind.strip().lower() == "web" and command.strip():
                return ["sh", "-lc", command.strip()]
    makefile = workdir / "Makefile"
    if makefile.is_file():
        content = makefile.read_text(errors="ignore")
        for target in ("dev", "run"):
            if re.search(rf"(?m)^{target}\s*:", content):
                return ["make", target]
    # PREFER WHAT THIS TASK BUILT. Selection used to walk a fixed filename list over the whole
    # repo, so a task that added `serve_nqueens.py` to a repo already containing the Sudoku
    # `serve.py` previewed SUDOKU — serve.py sorts earlier and serve_nqueens.py was not on the
    # list at all. The preview faithfully launched the wrong application (PR #62).
    # ONE decision, shared with the container path (see resolve_app_entry).
    kind, value = resolve_app_entry(workdir, changed)
    return command_for_entry(workdir, kind, value)


def command_for_entry(workdir: Path, kind: str, value: str) -> Optional[list[str]]:
    """The command that runs ONE resolved app entry. The only place an entry becomes a command.

    Extracted so the app BOSS declared at build time and the app a heuristic resolved at
    preview time go through identical construction. Two copies of this is the recurring bug in
    this file, not a hypothetical: `_detect_command` once built its own command from the repo
    root while `resolve_app_entry` was answering correctly, and every symptom of that — vite
    building a directory with no entry, a preview launching another task's app — was a second
    copy of this logic disagreeing with the first.
    """
    if kind == "node":
        # Run the app's OWN server, from its OWN directory, so `npm install` and the start
        # script resolve that app's manifest rather than the repository root's. The root check
        # above only ever looked at `workdir/package.json`, so an app built in a subdirectory
        # fell through to the static branch and was published without its backend.
        app_dir = "" if value in (".", "") else f"cd {shlex.quote(value)} && "
        app_root = workdir if value in (".", "") else workdir / value
        vite_start = False
        try:
            payload = json.loads((app_root / "package.json").read_text())
            scripts = payload.get("scripts") or {}
            deps = {**(payload.get("dependencies") or {}), **(payload.get("devDependencies") or {})}
            is_vite = (
                "vite" in deps
                or (app_root / "vite.config.ts").exists()
                or (app_root / "vite.config.js").exists()
            )
            vite_start = bool(is_vite and scripts.get("start"))
        except (OSError, json.JSONDecodeError):
            vite_start = False
        # Same relative-base Vite build as the root path: absolute /assets 404 under the
        # /v1/previews/<id>/web/ proxy prefix.
        build = "npx vite build --base=./ && " if vite_start else "npm run build --if-present && "
        return [
            "sh", "-lc",
            f"{app_dir}npm install --no-audit --no-fund --loglevel=error && "
            f"{build}npm start",
        ]
    if kind == "py":
        # python_entry_invocation decides script vs `-m` — a package module run as a script
        # cannot resolve its relative imports (crun_3e370c11).
        run_dir, argv = python_entry_invocation(workdir, value)
        if run_dir == ".":
            return argv
        return ["sh", "-lc", f"cd {shlex.quote(run_dir)} && {shlex.join(argv)}"]
    if kind == "static":
        # A static site still needs a SERVER — ES modules refuse to load over file:// — so a
        # bare index.html is a runnable app, not a library.
        return ["sh", "-lc",
                f'python3 -m http.server "$PORT" --bind 0.0.0.0 --directory {value}']
    return None


def _detect_verification(workdir: Path) -> Optional[str]:
    """A library/CLI/module build has no server to preview — the way you 'run' it is its test
    suite. Detect that so the preview can report an honest 'verified by tests' result instead of a
    misleading 'you forgot to add a preview command' error. Returns a human-readable test command."""
    package = workdir / "package.json"
    if package.is_file():
        try:
            scripts = (json.loads(package.read_text()).get("scripts") or {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        if scripts.get("test"):
            return "npm test"
    makefile = workdir / "Makefile"
    if makefile.is_file() and re.search(r"(?m)^test\s*:", makefile.read_text(errors="ignore")):
        return "make test"
    has_py_tests = (
        any(workdir.glob("test_*.py")) or any(workdir.glob("*_test.py"))
        or any(workdir.glob("**/test_*.py")) or (workdir / "tests").is_dir()
    )
    if has_py_tests:
        if (workdir / "pytest.ini").is_file() or (workdir / "pyproject.toml").is_file():
            return "python3 -m pytest -q"
        return "python3 -m unittest discover -v"
    return None


def _descendant_pids(pid: int) -> list[int]:
    pids = [pid]
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5).stdout
        for child in out.split():
            pids.extend(_descendant_pids(int(child)))
    except Exception:
        pass
    return pids


def _url_ok(url: str, *, headers: Optional[dict[str, str]] = None, check_body: bool = False) -> bool:
    try:
        request = urllib.request.Request(url, headers=headers or {})
        response = urllib.request.urlopen(request, timeout=2)
        status = int(response.status)
        if not (200 <= status < 400):
            return False
        # APP-4 (#94): the primary-route smoke must see real served content, not just a socket. A
        # blank body or a rendered error overlay on a 2xx no longer counts as healthy. (An SPA shell
        # is non-blank and not an error page, so it still passes — full render rubric is the headless gate.)
        if check_body and 200 <= status < 300:
            body = response.read(65536).decode("utf-8", "ignore")
            if not body.strip():
                return False
            from .visual_gate import is_error_page
            if is_error_page(body):
                return False
        return True
    except urllib.error.HTTPError:
        # A framework error page is not a healthy preview.
        return False
    except Exception:
        return False


def _http_ok(port: int) -> bool:
    return _url_ok(f"http://127.0.0.1:{port}", check_body=True)


def _diagnose_listening_port(log_path: str, port_hint: int) -> Optional[str]:
    """Why a port that IS listening still failed the health check.

    "app did not serve HTTP within timeout" describes two opposite situations and distinguishes
    neither: nothing ever bound (the build failed), or the app bound fine and answered the probe
    with something _http_ok rejects. The interview-scorecard app was the second — it serves
    /api/* and /metrics and has no route at all for "/", so the probe's 404 read as dead. The
    log said "Serving the interview scorecard app on http://0.0.0.0:47053" and the operator was
    told it never served, which sent everyone looking at the wrong half of the system.

    Returns a sentence naming the port and what "/" actually answered, or None when nothing is
    listening (in which case the generic timeout really is the right description).
    """
    for port in _candidate_ports(log_path, port_hint):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1.0)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                continue
        try:
            response = urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=2)
            status, body = int(response.status), response.read(4096)
            if not body.strip():
                return (f"the app IS listening on port {port}, but GET / returned {status} with "
                        f"an empty body, so there is no page to preview.")
            return (f"the app IS listening on port {port} and GET / returned {status}, but the "
                    f"health check rejected the page it served.")
        except urllib.error.HTTPError as http_error:
            return (f"the app IS listening on port {port}, but GET / returned "
                    f"{http_error.code}. It serves no page at the site root — check that the "
                    f"build produced a UI and not only an API.")
        except Exception:  # noqa: BLE001 — a port that will not answer is a port that failed
            return (f"the app IS listening on port {port} but GET / did not complete.")
    return None


def _classify_start_failure(message: str) -> tuple[str, str]:
    lowered = message.lower()
    # AN APP THAT IS LISTENING DID NOT TIME OUT. Checked before the generic "port" rule below,
    # which matches the word in this message too and labelled it port_timeout — telling the
    # operator to "ensure HOST/PORT are honored" about an app that honoured both and bound
    # exactly as asked. The fix here is a UI, not a port.
    if "no page at the site root" in lowered:
        return ("no_page_at_root",
                "The app started but serves nothing at '/'. Check that the build produced a UI "
                "and not only an API, then preview it again.")
    if "clone" in lowered:
        return "clone_failed", "Check repository access and the task branch, then retry."
    if "detect" in lowered or "command" in lowered:
        return "command_missing", "Add boss.preview.command or retry with an explicit command."
    if "did not serve" in lowered or "port" in lowered:
        return "port_timeout", "Inspect startup logs, ensure HOST/PORT are honored, or retry in Docker mode."
    return "boot_crash", "Inspect startup logs and retry in Docker mode."


def _candidate_ports(log_path: str, port_hint: int) -> list[int]:
    """Ordered candidates that unambiguously belong to THIS app:
    1) the URL/port the framework printed in its own log, then
    2) the PORT we injected via env (apps that honour it).
    Deliberately NOT process-tree lsof — on a dev box that picks up unrelated local services
    (Docker, Neo4j, IDEs) that also answer HTTP."""
    ordered: list[int] = []
    seen: set[int] = set()

    def add(p: int):
        if p not in seen:
            seen.add(p)
            ordered.append(p)

    try:
        log = open(log_path, errors="ignore").read()
        for m in re.finditer(r"https?://[^\s/]*?:(\d{2,5})\b", log):
            add(int(m.group(1)))
        for m in re.finditer(r"(?:port|listening on)\D{0,8}(\d{3,5})\b", log, re.I):
            add(int(m.group(1)))
    except Exception:
        pass
    add(port_hint)
    return ordered


#: Lines that carry the REASON a build died, as opposed to the frames underneath it.
_ERROR_LINE_MARKERS = (
    "error", "err!", "failed", "cannot find", "could not resolve", "not found",
    "unexpected", "syntaxerror", "typeerror", "referenceerror", "eaddrinuse",
    "permission denied", "enoent", "missing",
)


def _why_it_failed(log_path: str, limit: int = 900) -> str:
    """The part of a build log that says what went wrong.

    The old version kept the LAST 300 characters, which for a Node stack trace is the least
    informative end of the file — the operator got three frames of
    `rollup/dist/es/shared/parseAst.js` and no error message. Real example, repeated 49 times,
    with nothing anywhere naming the cause.

    So: surface the error LINES first, then the tail for context. A diagnostic that reliably
    omits the diagnosis is worse than no diagnostic, because it looks like one.
    """
    try:
        text = Path(log_path).read_text(errors="ignore")
    except Exception:  # noqa: BLE001
        return "log unavailable."
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "log was empty."
    culprits = [ln for ln in lines
                if any(m in ln.lower() for m in _ERROR_LINE_MARKERS)
                and not ln.lstrip().startswith("at ")]  # stack frames are not the reason
    parts = []
    if culprits:
        # The LAST few matching lines: a build often warns early and dies late.
        parts.append("error: " + " | ".join(culprits[-3:])[:limit])
    parts.append("log tail: " + " ".join(lines[-6:])[:limit])
    return " ".join(parts)


def _discover_working_port(log_path: str, port_hint: int, timeout: float = 20.0,
                           proc: Optional[subprocess.Popen] = None) -> Optional[int]:
    """Return the port the app actually serves HTTP on — its log-announced port first, then the
    injected PORT — each health-checked, so a neighboring local service is never mistaken for it.

    Given the process, stop waiting the moment it exits: an app that died on an ImportError at
    import time will never bind, and waiting out the full window only delays the real error."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in _candidate_ports(log_path, port_hint):
            if _http_ok(port):
                return port
        if proc is not None and proc.poll() is not None:
            # One last look: a server that forked and exited its parent may be up by now.
            for port in _candidate_ports(log_path, port_hint):
                if _http_ok(port):
                    return port
            return None
        time.sleep(0.8)
    return None


def launch_app(workdir: Path, command: list[str], log_path: str) -> tuple[subprocess.Popen, int]:
    """Start ONE app the way a preview does: PORT/HOST injected, output to log_path, low
    priority, its own process group. Returns (process, the PORT it was given).

    The single launcher. The build's serve proof (prove_app_serves) and the host preview both
    call it, so "it served when the build checked" and "it serves in preview" are the same
    claim about the same invocation, not two launchers that can drift apart.
    """
    port_hint = _free_port()
    # Point HOME + the npm cache at the (writable) workdir: the container's /home/boss/.npm is
    # owned by root (from the global claude-code install), so `npm install` in the preview would
    # fail with EACCES ("chown -R 100:101 /home/boss/.npm"). Keeps all npm writes inside workdir.
    env = {**os.environ, "PORT": str(port_hint), "HOST": "0.0.0.0", "PYTHONUNBUFFERED": "1",
           "HOME": str(workdir), "npm_config_cache": str(workdir / ".npm-cache"),
           "npm_config_fund": "false", "npm_config_audit": "false",
           "npm_config_update_notifier": "false"}
    logf = open(log_path, "w")
    try:
        proc = subprocess.Popen(_low_priority(command), cwd=str(workdir), env=env, stdout=logf,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    finally:
        logf.close()
    return proc, port_hint


def _serve_failure(log_path: str, port_hint: int) -> str:
    """Why an app did not serve, in the words the preview has always used. Call BEFORE killing
    it: once it is dead, "never bound" and "bound and served the wrong thing" look the same."""
    listening = _diagnose_listening_port(log_path, port_hint)
    if listening:
        return listening + " " + _why_it_failed(log_path)
    return "app did not serve HTTP within timeout. " + _why_it_failed(log_path)


# What the implement stage is told to produce. Kept here, beside the reader, so the prompt and
# find_run_declaration cannot describe two different files.
RUN_DECLARATION_INSTRUCTION = (
    "- HOW TO RUN IT (required if what you built serves HTTP — a web app, API or service): "
    "write a `boss.preview.command` file containing ONE shell command that starts the app "
    "and serves HTTP on the port in the `PORT` environment variable (bind 0.0.0.0). Put it in "
    "the app's own directory (the repository root for a root-level app); the command runs from "
    "that directory. For a Python module inside a package use `python3 -m pkg.module`, never "
    "`python3 pkg/module.py` — relative imports fail when a package module runs as a script. "
    "BOSS starts this exact command before the build can pass and fails the build, with the "
    "app's own error, if nothing answers HTTP. A library or CLI with no server writes no such "
    "file; its tests are how it is run.\n"
)


def _proof_tree(workdir: Path, overlay: Optional[dict] = None) -> Path:
    """A disposable copy of the build's tree to start the app in.

    In place would be wrong: the sandbox's diff is `git add -A`, so whatever the running app
    writes — a SQLite file, an npm cache, a log — would ride into the pull request. node_modules
    is linked rather than copied (it is large and the verify step already installed it), and
    `overlay` lays the files a micro-VM stage returned over a control-plane checkout that never
    received them.
    """
    proof = Path(tempfile.mkdtemp(prefix="boss-serve-proof-"))
    shutil.copytree(workdir, proof, dirs_exist_ok=True, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__",
                                                  ".npm-cache"))
    # os.walk with pruning, not rglob: rglob would descend every installed package.
    for dirpath, dirnames, _files in os.walk(workdir):
        if "node_modules" in dirnames:
            source = Path(dirpath) / "node_modules"
            target = proof / source.relative_to(workdir)
            if target.parent.is_dir() and not target.exists():
                try:
                    target.symlink_to(source, target_is_directory=True)
                except OSError:
                    pass
        dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git")]
    for rel, content in (overlay or {}).items():
        dest = proof / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    return proof


def prove_app_serves(workdir: Path, changed: Optional[list[str]], *, read=None,
                     serves_http: bool = False, overlay: Optional[dict] = None,
                     timeout: float = 180.0) -> dict:
    """START what the build built and require it to answer HTTP — before the build may pass.

    WHY. The Lead Triage Inbox build (crun_3e370c114b95425d8a7c, PR paramg/boss-sandbox#218)
    passed every gate and completed. Its preview then failed twice with "app did not serve HTTP
    within timeout … ImportError: attempted relative import with no known parent package". The
    app was never once started between "tests pass" and "done", so the first run of it was the
    operator's — and the error was reported as a preview timeout, about a build already called
    finished. Tests that import modules prove the modules; only starting the app proves the app.

    WHAT IS STARTED is what the preview will start, decided the same way:
      1. the run command the build declared (find_run_declaration), else
      2. the app derived from this build's own files (declare_app_entry → command_for_entry),
      3. and for a build that is a service but named neither, whatever the preview resolver
         would run — if that is ambiguous or nothing, the build fails asking for a declaration,
         because the preview would fail the same way later.
    A build that is not a service and declares nothing is a library/CLI: nothing to start, its
    tests are the proof (the preview's existing "verified by tests" path).

    Started with launch_app and waited on with _discover_working_port — the preview's own
    primitives — so the proof and the preview cannot disagree about how the app is run.

    Never raises. If the CHECK itself cannot run (the tree cannot be copied, the launcher
    fails), the record says `harness: true`: that is a fact about BOSS, not about the code, and
    the pipeline must not send the coding agent to fix it.
    """
    record: dict = {"ok": True, "proven": False, "source": "none", "command": None,
                    "port": None, "error": None, "app_entry": None, "verification": None,
                    "harness": False}
    tree = None
    try:
        tree = _proof_tree(workdir, overlay)
        command = None
        declaration = find_run_declaration(tree, changed)
        if declaration:
            command = run_declared_command(*declaration)
            record["source"] = "declared"
        else:
            entry = declare_app_entry(changed or [], read)
            parsed = parse_declared_app(entry) if entry else None
            if parsed:
                record["app_entry"] = entry
                command = command_for_entry(tree, *parsed)
                record["source"] = "derived"
            elif serves_http:
                try:
                    command = _detect_command(tree, changed)
                    record["source"] = "resolved"
                except AmbiguousAppError as exc:
                    record.update(ok=False, source="ambiguous", error=(
                        f"this build serves HTTP but did not declare how to run it, and the "
                        f"repository is ambiguous: {exc} Write boss.preview.command."))
                    return record
        if not command:
            if serves_http:
                record.update(ok=False, error=(
                    "this build serves HTTP but declares no way to run it and none could be "
                    "found. Write boss.preview.command with the command that starts it."))
            else:
                record.update(source="library", verification=_detect_verification(tree))
            return record

        record["command"] = " ".join(command)
        log_path = str(tree / ".boss-serve-proof.log")
        proc, port_hint = launch_app(tree, command, log_path)
        try:
            port = _discover_working_port(log_path, port_hint, timeout=timeout, proc=proc)
            if port is None:
                record.update(ok=False, error=_serve_failure(log_path, port_hint))
            else:
                record.update(proven=True, port=port)
        finally:
            _kill(proc.pid)
        return record
    except Exception as exc:  # noqa: BLE001 — the check reports, it never crashes the build
        record.update(ok=False, harness=True,
                      error=f"could not start the app to check it: "
                            f"{type(exc).__name__}: {exc}"[:600])
        return record
    finally:
        if tree is not None:
            shutil.rmtree(tree, ignore_errors=True)


class PreviewNotReady(Exception):
    """A preview was requested for a run that has not produced a reviewable change yet.

    Deliberately NOT a RuntimeError: the API tries several preview substrates in turn and
    treats RuntimeError as "this substrate failed, try the next one". Retrying three ways
    cannot conjure a PR, so this must propagate on the first attempt instead of being
    swallowed into a generic "preview could not start".
    """

    def __init__(self, run_id: str, status: str, phase: str) -> None:
        self.run_id, self.status, self.phase = run_id, status, phase
        super().__init__(
            f"run {run_id} has not opened a pull request yet "
            f"(status={status}, phase={phase}), so there is no completed change to preview"
        )

    def as_detail(self) -> dict:
        return {
            "error": "preview_unavailable_no_pr",
            "message": ("This run has not opened a pull request yet, so there is no completed "
                        "change to preview."),
            "run_id": self.run_id, "run_status": self.status, "run_phase": self.phase,
            "next_action": ("Wait for the run to finish. If it is blocked on a plan-approval "
                            "decision, approve it; if it failed, retry the task."),
        }


def _verify_green(run: dict) -> bool:
    state = run.get("state") or {}
    if state.get("tests_passed") is True:
        return True
    cmd = str(state.get("test_command") or "").lower()
    return state.get("test_exit_code") == 0 and "verify" in cmd


def find_local_run_workspace(run_id: str) -> Optional[Path]:
    """Retained local-subprocess sandbox for a verify-green run (no PR required)."""
    roots = [
        Path(tempfile.gettempdir()) / "boss-sandboxes",
        Path("/app"),
        Path("/var/lib/boss"),
        Path("/tmp"),
        PREVIEW_ROOT.parent,
    ]
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = str(root.resolve())
        except OSError:
            continue
        if resolved in seen or not root.is_dir():
            continue
        seen.add(resolved)
        for cand in root.glob(f"{run_id}*"):
            if not cand.is_dir():
                continue
            if (cand / "package.json").is_file() or (cand / "bugbox" / "package.json").is_file():
                return cand
    return None


def assert_previewable(run: dict, run_id: str) -> None:
    """A preview is meaningful once the work exists as a PR *or* verify-green files.

    Gated on the PR for GitHub-backed runs. A local-subprocess run that already
    passed ``npm run verify`` is also previewable from its retained workspace —
    review-timeout / no-PR must not hide working software.
    """
    if run.get("pull_request_url"):
        return
    if _verify_green(run) and find_local_run_workspace(run_id) is not None:
        return
    raise PreviewNotReady(run_id, str(run.get("status") or "?"), str(run.get("phase") or "?"))


def start_preview(engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60) -> dict:
    """Clone the run's branch, launch the app, discover its port, register an expiring preview."""
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    assert_previewable(run, run_id)
    spec = run.get("spec") or {}
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    # Iterations reuse the ROOT run's branch (boss/code/{root_run_id}); a run's own
    # id may have no branch. Prefer an explicit work_branch, else the root-derived one.
    _fctx = run.get("finalize_ctx") or {}
    branch = _fctx.get("work_branch") or f"boss/code/{_fctx.get('root_run_id') or run_id}"
    build_substrate = _fctx.get("execution_substrate") or normalize_build_substrate(
        settings.sandbox_provider
    ).value
    project_id = run["project_id"]
    preview_id = make_id("prev")

    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"{preview_id}-", dir=str(PREVIEW_ROOT)))
    log_path = str(workdir / "_preview.log")

    def _fail(msg: str) -> dict:
        failure_kind, suggested_action = _classify_start_failure(msg)
        with begin(engine) as c:
            c.execute(text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,substrate,build_substrate,"
                "status,error,failure_kind,log_path,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,:sub,:build,'failed',:e,:fk,:lg,now())"),
                {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}",
                 "b": branch, "sub": ExecutionSubstrate.PREVIEW_HOST.value,
                 "build": build_substrate, "e": msg[:500], "fk": failure_kind,
                 "lg": log_path})
            # A FAILURE NOBODY IS TOLD ABOUT.
            #
            # `app_preview.started` was emitted on success and nothing at all on failure, so a
            # preview that died wrote its reason into a row and stopped there. The Activity Log
            # is a projection of events — it cannot show what was never emitted — so an
            # operator sent to the log to find out why their preview failed found agent turns
            # and no preview at all, and reasonably concluded the log was useless.
            emit_event(
                c, project_id, "app_preview.failed", "operator", "app_preview", preview_id,
                {"run_id": run_id, "repo": f"{owner}/{repo}", "branch": branch,
                 "failure_kind": failure_kind, "suggested_action": suggested_action,
                 "error": msg[:500], "log_path": log_path},
            )
        return {"id": preview_id, "status": "failed", "error": msg,
                "failure_kind": failure_kind, "suggested_action": suggested_action}

    local_ws = None if (run.get("pull_request_url") or "") else find_local_run_workspace(run_id)
    if local_ws is not None:
        src = local_ws / "bugbox" if (local_ws / "bugbox" / "package.json").is_file() else local_ws
        try:
            shutil.copytree(
                src, workdir, dirs_exist_ok=True, ignore=shutil.ignore_patterns("node_modules")
            )
        except OSError as exc:
            return _fail(f"copy local workspace failed: {exc}")
    else:
        # NOT settings.github_token. That field is the old install-wide way of naming a
        # target repo and is empty wherever the repository was connected through the
        # wizard, so this handed git an anonymous URL for a private repo and every clone
        # died on "could not read Username for 'https://github.com'". See
        # github_connections.github_token_for.
        with begin(engine) as _c:
            token = github_token_for(_c, settings, project_id)
        from .repo_host import clone_url_for  # BOSS-267: host-aware clone
        clone_url = clone_url_for(getattr(settings, "target_repo_host", "github") or "github",
                                  owner, repo, token or "")
        clone = subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(workdir)],
                               capture_output=True, text=True, timeout=120)
        if clone.returncode != 0:
            return _fail(f"git clone failed: {(clone.stderr or '')[-300:]}")

    def _verify(cmd_str: str) -> dict:
        """Library/CLI/module build — there is no web server to serve. 'Run it' means run its
        tests: execute the suite one-shot, capture output, and return the result so the user sees
        it work, instead of a misleading 'add a preview command' error."""
        try:
            proc = subprocess.run(["sh", "-lc", cmd_str], cwd=str(workdir),
                                  capture_output=True, text=True, timeout=300)
            output = (proc.stdout or "") + (proc.stderr or "")
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            output, exit_code = "test run exceeded 300s and was stopped", 124
        try:
            Path(log_path).write_text(output, encoding="utf-8")
        except OSError:
            pass
        passed = exit_code == 0
        status = "verified" if passed else "failed"
        with begin(engine) as c:
            c.execute(text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,command,mode,substrate,"
                "build_substrate,status,failure_kind,error,log_path,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,:cmd,'test',:sub,:build,:st,:fk,:err,:lg,now())"),
                {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}",
                 "b": branch, "cmd": cmd_str, "sub": ExecutionSubstrate.PREVIEW_HOST.value,
                 "build": build_substrate, "st": status,
                 "fk": None if passed else "tests_failed",
                 "err": None if passed else "tests failed — see output", "lg": log_path})
        return {
            "id": preview_id, "status": status, "mode": "test", "kind": "library",
            "command": cmd_str, "exit_code": exit_code, "passed": passed,
            "output": output[-6000:],
            "message": (
                f"Verified by tests — NOT a live app preview. No runnable entrypoint was detected "
                f"(looked for: boss.preview.command, package.json dev/start, Procfile web, Makefile "
                f"dev/run, {'/'.join(_PY_ENTRYPOINTS)}, index.html in {'/'.join(_STATIC_DIRS[1:])}), "
                f"so this is either a library/module or an app whose entrypoint is named something "
                f"unrecognised. Ran its test suite (`{cmd_str}`): "
                f"{'PASSED ✓' if passed else 'FAILED ✗'} (exit {exit_code}). See the output below "
                f"and the PR to review the code."),
        }

    # `impl_files` is what THIS task built — prefer its entrypoint over any
    # pre-existing app already in the repo.
    _changed = lineage_impl_files(engine, run)
    command = _detect_command(workdir, _changed)
    if not command:
        # No web server, but a library still has a way to 'run it' — its tests. Prefer the test
        # command the pipeline already recorded on the run, else detect one from the repo.
        test_cmd = (run.get("state") or {}).get("test_command") or _detect_verification(workdir)
        if test_cmd:
            return _verify(test_cmd)
        return _fail(
            "no runnable app or test suite found in this build (add boss.preview.command, a "
            "package.json dev/start/test script, a Procfile web command, a Makefile dev/run/test "
            "target, or a Python entrypoint/tests)"
        )

    proc, port_hint = launch_app(workdir, command, log_path)

    # Web builds do `npm install` + `vite build` before the static server binds — well past the
    # default 20s window — so give discovery generous headroom (it returns as soon as the port
    # answers, so this never slows the fast case).
    port = _discover_working_port(log_path, port_hint, timeout=180.0)
    if port is None:
        # NAME WHICH FAILURE THIS IS before killing the process — once it is dead, the
        # difference between "never bound" and "bound and served the wrong thing" is gone.
        why = _serve_failure(log_path, port_hint)
        _kill(proc.pid)
        return _fail(why)

    url = f"http://127.0.0.1:{port}"
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO app_previews (id,project_id,run_id,repo,branch,command,port,url,pid,"
            "substrate,build_substrate,status,log_path,expires_at) "
            "VALUES (:id,:p,:r,:repo,:b,:cmd,:port,:url,:pid,:sub,:build,'running',:lg,:exp)"),
            {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}", "b": branch,
             "cmd": " ".join(command), "port": port, "url": url, "pid": proc.pid,
             "sub": ExecutionSubstrate.PREVIEW_HOST.value, "build": build_substrate,
             "lg": log_path, "exp": expires})
        emit_event(c, project_id, "app_preview.started", "operator", "app_preview", preview_id,
                   {"run_id": run_id, "url": url, "repo": f"{owner}/{repo}", "branch": branch})
    return publicize_preview({
        "id": preview_id, "status": "running", "url": url, "port": port,
        "pid": proc.pid, "expires_at": expires.isoformat(), "run_id": run_id,
        "repo": f"{owner}/{repo}", "branch": branch,
        "substrate": ExecutionSubstrate.PREVIEW_HOST.value,
        "build_substrate": build_substrate,
    }, settings=settings)


def _generate_dockerfile(workdir: Path, changed: Optional[list[str]] = None) -> int:
    """Ensure the app has a Dockerfile; return the container port the app listens on.
    Generated images set HOST=0.0.0.0 + PORT so deploy-ready apps bind correctly for -p mapping."""
    existing = workdir / "Dockerfile"
    if existing.exists():
        m = re.search(r"EXPOSE\s+(\d{2,5})", existing.read_text(errors="ignore"))
        return int(m.group(1)) if m else 8000
    # Prefer the entrypoint THIS task built. Sharing _PY_ENTRYPOINTS between the host and
    # container paths was not enough: the preference logic lived only in _detect_command, so a
    # docker-mode preview still picked the repo's pre-existing serve.py and launched Sudoku for
    # an N-Queens build (PR #62). Both paths must make the SAME choice, not merely consult the
    # same list.
    kind, value = resolve_app_entry(workdir, changed)
    if kind == "static":
        # Serve the static app THIS task built. Without this branch the container path fell
        # through to the repo-wide python entrypoint and launched the wrong application.
        port = 8000
        existing.write_text(
            "FROM python:3.11-slim\nWORKDIR /app\nCOPY . .\n"
            f"ENV HOST=0.0.0.0 PORT={port}\nEXPOSE {port}\n"
            f'CMD ["python3", "-m", "http.server", "{port}", "--bind", "0.0.0.0", '
            f'"--directory", "{value}"]\n')
        return port
    py_entry = value if kind == "py" else None
    if py_entry:
        port = 8000
        reqs = "COPY requirements.txt ./\nRUN pip install --no-cache-dir -r requirements.txt\n" \
            if (workdir / "requirements.txt").exists() else ""
        # Same invocation as the host path: a package module runs with -m from above its
        # package, or its relative imports fail exactly as they did on the host.
        run_dir, argv = python_entry_invocation(workdir, py_entry)
        cmd = ", ".join(json.dumps(a) for a in argv)
        app_dir = "/app" if run_dir == "." else f"/app/{run_dir}"
        existing.write_text(
            "FROM python:3.11-slim\nWORKDIR /app\n" + reqs +
            f"COPY . .\nWORKDIR {app_dir}\n"
            f"ENV HOST=0.0.0.0 PORT={port} PYTHONUNBUFFERED=1\nEXPOSE {port}\n"
            f'CMD [{cmd}]\n')
        return port
    if kind == "node":
        port = 3000
        app_workdir = "/app" if value in (".", "") else f"/app/{value}"
        existing.write_text(
            "FROM node:20-slim\n"
            "COPY . /app\n"
            f"WORKDIR {app_workdir}\n"
            "RUN npm install --no-audit --no-fund --loglevel=error\n"
            "RUN npm run build --if-present || true\n"
            f"ENV HOST=0.0.0.0 PORT={port}\nEXPOSE {port}\n"
            "CMD [\"npm\", \"run\", \"start\"]\n")
        return port
    if (workdir / "package.json").exists():
        port = 3000
        existing.write_text(
            "FROM node:20-slim\nWORKDIR /app\nCOPY package*.json ./\n"
            "RUN npm install --no-audit --no-fund --loglevel=error\nCOPY . .\n"
            "RUN npm run build --if-present || true\n"
            f"ENV HOST=0.0.0.0 PORT={port}\nEXPOSE {port}\nCMD [\"npm\", \"run\", \"start\"]\n")
        return port
    raise RuntimeError("no Dockerfile and could not infer a stack (server.py/app.py/package.json)")


def start_container_preview(engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60) -> dict:
    """Tier-2 dev-container preview: build a Docker image from the run's branch and run it with a
    mapped port — isolated, reproducible, the same image that later ships to remote compute."""
    if shutil.which("docker") is None:
        raise RuntimeError("docker is not available on this host")
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    assert_previewable(run, run_id)
    spec = run.get("spec") or {}
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    # Iterations reuse the ROOT run's branch (boss/code/{root_run_id}); a run's own
    # id may have no branch. Prefer an explicit work_branch, else the root-derived one.
    _fctx = run.get("finalize_ctx") or {}
    branch = _fctx.get("work_branch") or f"boss/code/{_fctx.get('root_run_id') or run_id}"
    build_substrate = _fctx.get("execution_substrate") or normalize_build_substrate(
        settings.sandbox_provider
    ).value
    project_id = run["project_id"]
    preview_id = make_id("prev")
    tag = f"boss-prev-{preview_id.replace('_', '-').lower()}"
    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"{preview_id}-", dir=str(PREVIEW_ROOT)))

    def _fail(msg: str) -> dict:
        failure_kind, suggested_action = _classify_start_failure(msg)
        with begin(engine) as c:
            c.execute(text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,mode,substrate,"
                "build_substrate,status,error,failure_kind,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,'docker',:sub,:build,'failed',:e,:fk,now())"),
                {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}",
                 "b": branch, "sub": ExecutionSubstrate.PREVIEW_DOCKER.value,
                 "build": build_substrate, "e": msg[:500], "fk": failure_kind})
            # Same reason as the host path: a failure that emits nothing cannot appear in a log
            # that is a projection of events.
            emit_event(
                c, project_id, "app_preview.failed", "operator", "app_preview", preview_id,
                {"run_id": run_id, "repo": f"{owner}/{repo}", "branch": branch, "mode": "docker",
                 "failure_kind": failure_kind, "suggested_action": suggested_action,
                 "error": msg[:500]},
            )
        return {"id": preview_id, "status": "failed", "error": msg, "mode": "docker",
                "failure_kind": failure_kind, "suggested_action": suggested_action}

    # NOT settings.github_token — see github_connections.github_token_for. Empty wherever the
    # repository was connected through the wizard, which made every clone of a private repo
    # fail on "could not read Username for 'https://github.com'".
    with begin(engine) as _c:
        token = github_token_for(_c, settings, project_id)
    from .repo_host import clone_url_for  # BOSS-267: host-aware clone
    clone_url = clone_url_for(getattr(settings, "target_repo_host", "github") or "github",
                              owner, repo, token or "")
    clone = subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(workdir)],
                           capture_output=True, text=True, timeout=120)
    if clone.returncode != 0:
        return _fail(f"git clone failed: {(clone.stderr or '')[-300:]}")

    try:
        _changed_ep = lineage_impl_files(engine, run)
        container_port = _generate_dockerfile(workdir, _changed_ep)
    except RuntimeError as exc:
        return _fail(str(exc))

    build = subprocess.run(["docker", "build", "-t", tag, str(workdir)],
                           capture_output=True, text=True, timeout=600)
    if build.returncode != 0:
        return _fail(f"docker build failed: {(build.stderr or build.stdout or '')[-400:]}")

    host_port = _free_port()
    run_cmd = subprocess.run(
        ["docker", "run", "-d", "--name", tag, "-p", f"{host_port}:{container_port}",
         "-e", f"PORT={container_port}", "-e", "HOST=0.0.0.0", tag],
        capture_output=True, text=True, timeout=60)
    if run_cmd.returncode != 0:
        return _fail(f"docker run failed: {(run_cmd.stderr or '')[-300:]}")
    container_id = run_cmd.stdout.strip()[:12]

    url = f"http://127.0.0.1:{host_port}"
    ok = False
    for _ in range(20):
        if _http_ok(host_port):
            ok = True
            break
        time.sleep(1.0)
    if not ok:
        logs = subprocess.run(["docker", "logs", "--tail", "20", tag], capture_output=True, text=True).stdout
        subprocess.run(["docker", "rm", "-f", tag], capture_output=True)
        return _fail(f"container did not serve HTTP. logs: {logs[-300:]}")

    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO app_previews (id,project_id,run_id,repo,branch,mode,container_id,port,url,"
            "substrate,build_substrate,status,expires_at) "
            "VALUES (:id,:p,:r,:repo,:b,'docker',:cid,:port,:url,:sub,:build,'running',:exp)"),
            {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}", "b": branch,
             "cid": container_id, "port": host_port, "url": url,
             "sub": ExecutionSubstrate.PREVIEW_DOCKER.value, "build": build_substrate,
             "exp": expires})
        emit_event(c, project_id, "app_preview.started", "operator", "app_preview", preview_id,
                   {"run_id": run_id, "url": url, "mode": "docker", "image": tag})
    return publicize_preview({
        "id": preview_id, "status": "running", "url": url, "port": host_port, "mode": "docker",
        "container_id": container_id, "image": tag, "expires_at": expires.isoformat(),
        "run_id": run_id, "repo": f"{owner}/{repo}", "branch": branch,
        "substrate": ExecutionSubstrate.PREVIEW_DOCKER.value,
        "build_substrate": build_substrate,
    }, settings=settings)


def start_rootfs_preview(
    engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60,
    command: str = "",
) -> dict:
    """Serve a micro-VM run from its retained build rootfs (no host-side re-clone)."""
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    assert_previewable(run, run_id)
    fctx = run.get("finalize_ctx") or {}
    state = run.get("state") or {}
    build_substrate = fctx.get("execution_substrate") or state.get("execution_substrate")
    if build_substrate != ExecutionSubstrate.MICROVM.value:
        raise RuntimeError("rootfs preview is only available for a micro-VM build")
    sandbox_ids = fctx.get("sandbox_task_ids") or state.get("sandbox_task_ids") or []
    sandbox_id = fctx.get("sandbox_task_id") or (sandbox_ids[-1] if sandbox_ids else None)
    if not sandbox_id:
        raise RuntimeError("micro-VM build has no retained sandbox_task_id")

    provider = MicroVMProvider(settings)
    provider.hold(
        sandbox_id,
        ttl_seconds=max(60, int(ttl_minutes) * 60),
        reason=f"rootfs preview for {run_id}",
    )
    served = provider.serve_preview(
        sandbox_id,
        ttl_seconds=max(60, int(ttl_minutes) * 60),
        command=command,
    )
    remote_url = served.get("url") or served.get("proxy_url")
    if not remote_url:
        raise RuntimeError("sandbox service did not return a rootfs preview URL")
    preview_id = make_id("prev")
    spec = run.get("spec") or {}
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    branch = fctx.get("work_branch") or f"boss/code/{fctx.get('root_run_id') or run_id}"
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    with begin(engine) as c:
        c.execute(
            text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,command,url,mode,"
                "substrate,build_substrate,sandbox_task_id,status,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,:cmd,:url,'rootfs',:sub,:build,:sid,'running',:exp)"
            ),
            {"id": preview_id, "p": run["project_id"], "r": run_id,
             "repo": f"{owner}/{repo}", "b": branch, "cmd": served.get("command") or command,
             "url": remote_url, "sub": ExecutionSubstrate.PREVIEW_ROOTFS.value,
             "build": build_substrate, "sid": sandbox_id, "exp": expires},
        )
        emit_event(
            c, run["project_id"], "app_preview.started", "operator", "app_preview", preview_id,
            {"run_id": run_id, "mode": "rootfs", "sandbox_task_id": sandbox_id,
             "substrate": ExecutionSubstrate.PREVIEW_ROOTFS.value},
        )
    return publicize_preview({
        "id": preview_id, "status": "running", "mode": "rootfs", "url": remote_url,
        "substrate": ExecutionSubstrate.PREVIEW_ROOTFS.value,
        "build_substrate": build_substrate, "sandbox_task_id": sandbox_id,
        "expires_at": expires.isoformat(), "run_id": run_id,
        "repo": f"{owner}/{repo}", "branch": branch,
    }, settings=settings)


def _process_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _docker_alive(container_id: Optional[str]) -> bool:
    if not container_id or shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
        capture_output=True, text=True, timeout=5,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _respawn_process_preview(engine, settings: Settings, preview_id: str) -> None:
    """Re-launch a dead process-mode preview IN PLACE — same preview id, so its
    /v1/previews/<id>/web URL survives an api restart/redeploy. Re-clones the branch, rebuilds,
    serves, then flips the SAME row back to 'running' with the new pid/port. Runs in a background
    thread (the clone+build takes ~60s) so it never blocks api boot."""
    try:
        with begin(engine) as c:
            row = c.execute(text(
                "SELECT run_id,project_id,repo,branch,build_substrate,expires_at FROM app_previews WHERE id=:id"
            ), {"id": preview_id}).mappings().first()
        if row is None:
            return
        project_id = row["project_id"]
        repo_full = row["repo"] or ""
        owner, _, repo = repo_full.partition("/")
        branch = row["branch"]
        if not owner or not repo or not branch:
            return
        PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix=f"{preview_id}-", dir=str(PREVIEW_ROOT)))
        log_path = str(workdir / "_preview.log")
        # NOT settings.github_token. That field is the old install-wide way of naming a
        # target repo and is empty wherever the repository was connected through the
        # wizard, so this handed git an anonymous URL for a private repo and every clone
        # died on "could not read Username for 'https://github.com'". See
        # github_connections.github_token_for.
        with begin(engine) as _c:
            token = github_token_for(_c, settings, project_id)
        from .repo_host import clone_url_for  # BOSS-267: host-aware clone
        clone_url = clone_url_for(getattr(settings, "target_repo_host", "github") or "github",
                                  owner, repo, token or "")
        clone = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(workdir)],
            capture_output=True, text=True, timeout=120)
        if clone.returncode != 0:
            return
        _changed = lineage_impl_files(engine, {"id": row["run_id"]})
        command = _detect_command(workdir, _changed)
        if not command:
            return
        port_hint = _free_port()
        env = {**os.environ, "PORT": str(port_hint), "HOST": "0.0.0.0", "PYTHONUNBUFFERED": "1",
               "HOME": str(workdir), "npm_config_cache": str(workdir / ".npm-cache"),
               "npm_config_fund": "false", "npm_config_audit": "false",
               "npm_config_update_notifier": "false"}
        logf = open(log_path, "w")
        proc = subprocess.Popen(_low_priority(command), cwd=str(workdir), env=env, stdout=logf,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        logf.close()
        port = _discover_working_port(log_path, port_hint, timeout=180.0)
        if port is None:
            _kill(proc.pid)
            return
        url = f"http://127.0.0.1:{port}"
        with begin(engine) as c:
            c.execute(text(
                "UPDATE app_previews SET status='running', pid=:pid, port=:port, url=:url, "
                "log_path=:lg WHERE id=:id"),
                {"pid": proc.pid, "port": port, "url": url, "lg": log_path, "id": preview_id})
    except Exception:  # noqa: BLE001 — best-effort; a failed respawn just leaves it 'stopped'
        return


# How many dead process previews to auto-respawn on boot (newest first) — bounds the rebuild
# cost so a restart with many historical previews doesn't stampede npm builds.
_RESPAWN_ON_BOOT_LIMIT = 3


def reconcile_previews(engine, settings: Settings) -> dict[str, int]:
    """Idempotently reconcile durable preview rows against their live substrate."""
    with begin(engine) as c:
        rows = c.execute(text(
            "SELECT id,pid,mode,container_id,url,expires_at,sandbox_task_id FROM app_previews "
            "WHERE status='running'"
        )).mappings().all()
    now = datetime.now(timezone.utc)
    alive = stopped = expired = 0
    headers = ({"X-Sandbox-Token": settings.sandbox_token}
               if settings.sandbox_token else {})
    respawn_candidates: list[str] = []   # dead process-mode previews to relaunch in place
    for raw in rows:
        row = dict(raw)
        expires_at = row.get("expires_at")
        mode = row.get("mode") or "process"
        if expires_at and expires_at <= now:
            target_status = "expired"
        else:
            substrate_alive = (
                _docker_alive(row.get("container_id")) if mode == "docker"
                else _url_ok(row.get("url") or "", headers=headers) if mode == "rootfs"
                else _process_alive(row.get("pid"))
            )
            healthy = substrate_alive and _url_ok(
                row.get("url") or "", headers=headers if mode == "rootfs" else None
            )
            target_status = "running" if healthy else "stopped"
        if target_status == "running":
            alive += 1
            continue
        if target_status == "expired":
            _teardown(row, settings)
        with begin(engine) as c:
            c.execute(
                text("UPDATE app_previews SET status=:status WHERE id=:id AND status='running'"),
                {"status": target_status, "id": row["id"]},
            )
        if target_status == "expired":
            expired += 1
        else:
            stopped += 1
            # A process preview (web app) that died with the old container — respawn it IN PLACE so
            # its URL survives the restart. rootfs/docker/test modes reconcile on their own.
            if mode == "process":
                respawn_candidates.append(row["id"])

    # Bound the rebuild cost: only relaunch the newest few (they're the ones anyone is looking at).
    respawned = 0
    if respawn_candidates:
        with begin(engine) as c:
            ordered = [r[0] for r in c.execute(text(
                "SELECT id FROM app_previews WHERE id = ANY(:ids) ORDER BY created_at DESC LIMIT :lim"
            ), {"ids": respawn_candidates, "lim": _RESPAWN_ON_BOOT_LIMIT}).all()]
        for pid in ordered:
            threading.Thread(target=_respawn_process_preview, args=(engine, settings, pid),
                             daemon=True).start()
            respawned += 1
    return {"checked": len(rows), "running": alive, "stopped": stopped, "expired": expired,
            "respawning": respawned}


def preview_log_snapshot(engine, preview_id: str, *, tail_chars: int = 12000) -> dict:
    with begin(engine) as c:
        row = c.execute(text(
            "SELECT status,mode,log_path,container_id,failure_kind,error FROM app_previews WHERE id=:id"
        ), {"id": preview_id}).mappings().first()
    if row is None:
        raise ValueError("preview not found")
    content = ""
    if row["log_path"]:
        try:
            content = Path(row["log_path"]).read_text(errors="ignore")[-tail_chars:]
        except OSError:
            content = ""
    elif row["mode"] == "docker" and row["container_id"] and shutil.which("docker"):
        result = subprocess.run(
            ["docker", "logs", "--tail", "200", row["container_id"]],
            capture_output=True, text=True, timeout=10,
        )
        content = (result.stdout + result.stderr)[-tail_chars:]
    return {"preview_id": preview_id, "status": row["status"], "mode": row["mode"],
            "failure_kind": row["failure_kind"], "error": row["error"], "log": content}


def stream_preview_logs(engine, preview_id: str):
    """SSE generator that tails startup logs until the preview reaches a terminal state."""
    offset = 0
    for _ in range(600):  # bounded to ten minutes; clients may reconnect
        snapshot = preview_log_snapshot(engine, preview_id)
        content = snapshot["log"]
        if len(content) > offset:
            chunk = content[offset:]
            offset = len(content)
            yield f"event: log\ndata: {json.dumps(chunk)}\n\n"
        else:
            yield ": heartbeat\n\n"
        if snapshot["status"] in {"failed", "stopped", "expired"}:
            yield f"event: end\ndata: {json.dumps(snapshot['status'])}\n\n"
            return
        time.sleep(1)


def _docker_rm(container_id: Optional[str]) -> None:
    if container_id:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)


def _kill(pid: Optional[int]) -> None:
    if not pid:
        return
    try:
        os.killpg(os.getpgid(pid), 9)
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def _inject_shim(html: bytes, prefix: str) -> bytes:
    """Make a path-prefixed preview behave like an app mounted at `/`.

    The server-side rewrite handles initial markup/CSS; the client shim covers
    dynamic DOM, fetch/XHR, history routing, WebSocket/EventSource, and captures
    preview-anchored operator feedback.
    """
    prefix = prefix.rstrip("/")
    p_json = json.dumps(prefix)
    feedback_url = json.dumps(prefix.rsplit("/web", 1)[0] + "/feedback")
    shim = (
        "<base href=\"" + prefix + "/\">"
        "<script data-boss-preview-shim>(function(){var P=" + p_json + ",F=" + feedback_url + ";"
        "function path(u){if(typeof u!==\"string\")return u;"
        "if(u.charAt(0)===\"/\"&&u.slice(0,2)!==\"//\"&&u.indexOf(P)!==0)return P+u;return u;}"
        "function srcset(v){return String(v||\"\").split(\",\").map(function(x){var p=x.trim().split(/\\s+/);"
        "p[0]=path(p[0]);return p.join(\" \").trim();}).join(\", \");}"
        "function ws(u){u=path(u);if(typeof u===\"string\"&&u.charAt(0)===\"/\")"
        "return(location.protocol===\"https:\"?\"wss:\":\"ws:\")+\"//\"+location.host+u;return u;}"
        "var of=window.fetch;window.fetch=function(u,o){return of.call(this,path(u),o);};"
        "var xo=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){"
        "arguments[1]=path(u);return xo.apply(this,arguments);};"
        "[\"pushState\",\"replaceState\"].forEach(function(k){var old=history[k];"
        "history[k]=function(s,t,u){return old.call(this,s,t,path(u));};});"
        "var OW=window.WebSocket;if(OW){window.WebSocket=function(u,p){"
        "return p===undefined?new OW(ws(u)):new OW(ws(u),p);};window.WebSocket.prototype=OW.prototype;}"
        "var OE=window.EventSource;if(OE){window.EventSource=function(u,o){return new OE(path(u),o);};"
        "window.EventSource.prototype=OE.prototype;}"
        "function fix(root){if(!root||!root.querySelectorAll)return;"
        "root.querySelectorAll(\"[href],[src],[action],[srcset],[style]\").forEach(function(el){"
        "[\"href\",\"src\",\"action\"].forEach(function(a){var v=el.getAttribute(a);"
        "if(v)el.setAttribute(a,path(v));});var ss=el.getAttribute(\"srcset\");if(ss)el.setAttribute(\"srcset\",srcset(ss));"
        "var st=el.getAttribute(\"style\");if(st)el.setAttribute(\"style\",st.replace(/url\\((['\"]?)\\/(?!\\/)/g,\"url($1\"+P+\"/\"));});}"
        "new MutationObserver(function(ms){ms.forEach(function(m){m.addedNodes.forEach(fix);});})"
        ".observe(document.documentElement,{childList:true,subtree:true});"
        "document.addEventListener(\"DOMContentLoaded\",function(){fix(document);"
        "if(document.querySelector(\"[data-boss-feedback]\"))return;var b=document.createElement(\"button\");"
        "b.textContent=\"Send preview feedback\";b.setAttribute(\"data-boss-feedback\",\"1\");"
        "b.style.cssText=\"position:fixed;right:16px;bottom:16px;z-index:2147483647;padding:9px 13px;"
        "border:0;border-radius:999px;background:#111827;color:white;font:600 12px system-ui;cursor:pointer\";"
        "b.onclick=async function(){var message=prompt(\"What should BOSS change here?\");if(!message)return;"
        "var route=location.pathname.indexOf(P)===0?(location.pathname.slice(P.length)||\"/\"):location.pathname;"
        "var a=document.activeElement,sel=a?String(a.tagName||\"\").toLowerCase()+"
        "(a.id?\"#\"+a.id:\"\")+(a.className&&typeof a.className===\"string\"?\".\"+a.className.trim().replace(/\\s+/g,\".\"):\"\"):\"\";"
        "var r=await of.call(window,F,{method:\"POST\",headers:{\"Content-Type\":\"application/json\"},"
        "body:JSON.stringify({message:message,anchor:route,selected_element:sel})});"
        "var d=await r.json();alert(d.question||\"Feedback captured. Confirm the proposed iteration in BOSS.\");};"
        "document.body.appendChild(b);});})();</script>"
    )
    try:
        s = html.decode("utf-8", "ignore")
        # Initial static resources and CSS URLs must be prefixed before the browser
        # starts fetching them. Protocol-relative/external/fragment URLs are untouched.
        def attr_rewrite(match: re.Match) -> str:
            value = match.group("value")
            if value.startswith("/") and not value.startswith("//") and not value.startswith(prefix):
                value = prefix + value
            return f"{match.group('attr')}={match.group('quote')}{value}{match.group('quote')}"

        s = re.sub(
            r"(?P<attr>href|src|action)=(?P<quote>['\"])(?P<value>[^'\"]*)(?P=quote)",
            attr_rewrite, s, flags=re.I,
        )
        def srcset_rewrite(match: re.Match) -> str:
            candidates = []
            for candidate in match.group("value").split(","):
                pieces = candidate.strip().split()
                if pieces and pieces[0].startswith("/") and not pieces[0].startswith("//"):
                    pieces[0] = prefix + pieces[0]
                candidates.append(" ".join(pieces))
            return f"srcset={match.group('quote')}{', '.join(candidates)}{match.group('quote')}"

        s = re.sub(
            r"srcset=(?P<quote>['\"])(?P<value>[^'\"]*)(?P=quote)",
            srcset_rewrite, s, flags=re.I,
        )
        s = re.sub(r"url\((['\"]?)/(?!/)", rf"url(\1{prefix}/", s, flags=re.I)
        if re.search(r"<head(?:\s[^>]*)?>", s, flags=re.I):
            s = re.sub(r"(<head(?:\s[^>]*)?>)", lambda m: m.group(1) + shim, s, count=1, flags=re.I)
        elif "<html" in s.lower():
            s = re.sub(r"(<html[^>]*>)", lambda m: m.group(1) + "<head>" + shim + "</head>", s,
                       count=1, flags=re.I)
        else:
            s = shim + s
        return s.encode("utf-8")
    except Exception:  # noqa: BLE001
        return html


# HEADERS THE PREVIEWED APP MUST NOT SEE. Everything else is forwarded.
#
# This was an ALLOWLIST of four names, and it broke every app that invented a header of its
# own. The first casualty was ApproveIt's X-Acting-User-Id, which was fixed by appending it to
# the list — the second was an app whose client sends X-Request-ID as an idempotency key on
# POST /api/verifications. Its server, correctly, refuses a request without one:
#
#     ❌ X-Request-ID header is required
#
# The app was right and its client did send the header. The PROXY removed it. An operator sees
# an app BOSS built failing in a preview BOSS serves, with an error that blames the app.
#
# An allowlist cannot work here. BOSS does not know what the apps it writes will invent, and
# adding a name per app means every new app is broken until somebody notices. The real
# requirement is narrow and knowable in advance — do not hand the tenant process BOSS's own
# credentials, and do not forward headers that describe THIS hop rather than the request. So
# the rule is inverted: forward everything except those.
#
# Authorization and X-Boss-Api-Key are the control-plane secrets. Cookie is denied for the
# same reason and it matters more than it looks: previews are served from the API's own origin,
# so the browser attaches BOSS's cookies to preview requests, and forwarding them would hand a
# tenant app the operator's session.
_PREVIEW_BLOCKED_HEADER_NAMES = frozenset({
    # BOSS credentials. Never.
    "authorization", "proxy-authorization", "cookie",
    "x-boss-api-key", "x-ops-token", "x-api-key",
    # This hop, not the request. Forwarding them corrupts the upstream request: the app would
    # be told the wrong Host and a Content-Length that no longer matches the body we send.
    "host", "content-length", "connection", "keep-alive", "proxy-connection",
    "transfer-encoding", "te", "trailer", "upgrade", "expect",
})


def forwarded_preview_headers(
    incoming: Optional[dict] = None,
    *,
    content_type: Optional[str] = None,
) -> dict[str, str]:
    """The incoming headers a preview app may receive: everything not explicitly blocked.

    See _PREVIEW_BLOCKED_HEADER_NAMES for why this is a denylist. In short: an allowlist has to
    be extended for every header an app invents, and until it is, the app fails in the preview
    with an error that reads as the app's fault.
    """
    out: dict[str, str] = {}
    for key, value in (incoming or {}).items():
        if value is None:
            continue
        name = str(key).strip()
        if not name or name.lower() in _PREVIEW_BLOCKED_HEADER_NAMES:
            continue
        out[name] = str(value)
    if content_type and "content-type" not in {k.lower() for k in out}:
        out["Content-Type"] = content_type
    return out


def proxy_preview(engine, preview_id: str, subpath: str, method: str,
                  query_string: str = "", body: Optional[bytes] = None,
                  content_type: Optional[str] = None,
                  sandbox_token: str = "",
                  incoming_headers: Optional[dict] = None):
    """Reverse-proxy a request to a running preview's loopback port → (status, content_type, bytes).
    Only the api can reach the preview's 127.0.0.1 port, so this exposes it via the api's own
    (tailnet-routed) URL. HTML responses get the root-relative path-rewrite shim."""
    import urllib.error
    with begin(engine) as c:
        row = c.execute(text("SELECT url,status,mode FROM app_previews WHERE id=:id"),
                        {"id": preview_id}).mappings().first()
    if row is None:
        return 404, "text/plain", b"preview not found", {}
    if row["status"] != "running":
        return 409, "text/plain", f"preview not running (status={row['status']})".encode(), {}
    if str(row.get("mode") or "") == FIXTURE_MODE:
        if subpath and subpath not in {"", "/"}:
            return 404, "text/plain", b"fixture preview has no extra paths", {}
        return 200, "text/html; charset=utf-8", fixture_html(preview_id), {}
    if not row["url"]:
        return 409, "text/plain", f"preview not running (status={row['status']})".encode(), {}
    target = row["url"].rstrip("/") + "/" + (subpath or "")
    if query_string:
        target += "?" + query_string
    req = urllib.request.Request(target, data=body or None, method=method)
    for header_name, header_value in forwarded_preview_headers(
        incoming_headers, content_type=content_type
    ).items():
        req.add_header(header_name, header_value)
    if row["mode"] == "rootfs" and sandbox_token:
        req.add_header("X-Sandbox-Token", sandbox_token)
    response_headers: dict[str, str] = {}
    try:
        resp = urllib.request.build_opener(_NoRedirect()).open(req, timeout=25)
        status = resp.status
        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        data = resp.read()
        if resp.headers.get("Location"):
            response_headers["Location"] = resp.headers["Location"]
    except urllib.error.HTTPError as e:
        status, ctype, data = e.code, e.headers.get("Content-Type", "text/plain"), e.read()
        if e.headers.get("Location"):
            response_headers["Location"] = e.headers["Location"]
    except Exception as e:  # noqa: BLE001 — process gone (e.g. after an api restart): self-heal
        try:
            with begin(engine) as c2:
                c2.execute(text("UPDATE app_previews SET status='stopped' WHERE id=:id AND status='running'"),
                           {"id": preview_id})
        except Exception:
            pass
        return 502, "text/html", (
            "<div style=\"font-family:system-ui;padding:24px;max-width:520px\">"
            "<h3>Preview is no longer running</h3>"
            "<p>Its process ended (this happens after a redeploy). Go back to the code task and click "
            "<b>Run preview</b> to regenerate it.</p>"
            f"<p style=\"color:#888;font-size:12px\">{e}</p></div>"
        ).encode(), {}
    location = response_headers.get("Location")
    if location and location.startswith("/") and not location.startswith("//"):
        response_headers["Location"] = f"/v1/previews/{preview_id}/web{location}"
    if "text/html" in (ctype or ""):
        data = _inject_shim(data, f"/v1/previews/{preview_id}/web")
    return status, ctype, data, response_headers


def smoke_test(
    engine,
    preview_id: str,
    checks: Optional[list[dict]] = None,
    settings: Optional[Settings] = None,
) -> dict:
    """Tier-3 smoke canary: hit the live preview URL with a set of checks and assert each — proves
    the deployed app actually works, not just that it serves a page. Records a receipt event.
    Each check: {method, path, body?, expect_status?, expect_contains?}. Default: GET / -> 2xx."""
    import urllib.request
    with begin(engine) as c:
        row = c.execute(text("SELECT project_id,url,status,run_id FROM app_previews WHERE id=:id"),
                        {"id": preview_id}).mappings().first()
    if row is None:
        raise ValueError("preview not found")
    if row["status"] != "running" or not row["url"]:
        raise ValueError(f"preview not running (status={row['status']})")
    base = row["url"].rstrip("/")
    checks = checks or [{"method": "GET", "path": "/", "expect_status": 200}]
    results, passed = [], 0
    for chk in checks:
        method = (chk.get("method") or "GET").upper()
        path = chk.get("path") or "/"
        if method not in {"GET", "HEAD"} or not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ValueError("preview smoke supports only GET/HEAD on relative paths")
        want_status = chk.get("expect_status")
        want_contains = chk.get("expect_contains")
        data = json.dumps(chk["body"]).encode() if chk.get("body") is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        ok, status, detail = False, None, ""
        try:
            resp = urllib.request.urlopen(req, timeout=8)
            status = resp.status
            text_body = resp.read(8000).decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            status = e.code
            text_body = (e.read(2000).decode("utf-8", "ignore") if hasattr(e, "read") else "")
        except Exception as e:  # noqa: BLE001
            detail = str(e)[:160]
            text_body = ""
        if status is not None:
            ok = ((want_status is None or status == want_status)
                  and (want_contains is None or want_contains in text_body))
            if not ok and not detail:
                detail = (f"status {status}" + (f" != {want_status}" if want_status and status != want_status else "")
                          + ("; missing expected text" if want_contains and want_contains not in text_body else ""))
        results.append({"method": method, "path": path, "status": status, "ok": ok, "detail": detail})
        if ok:
            passed += 1
    all_ok = passed == len(checks)
    with begin(engine) as c:
        emit_event(c, row["project_id"], "app_preview.smoke", "operator", "app_preview", preview_id,
                   {"run_id": row["run_id"], "passed": passed, "total": len(checks),
                    "ok": all_ok, "results": results})
    return {
        "preview_id": preview_id,
        "url": operator_preview_open_url(preview_id, settings=settings),
        "internal_url": base,
        "open_url": operator_preview_open_url(preview_id, settings=settings),
        "ok": all_ok,
        "passed": passed,
        "total": len(checks),
        "results": results,
    }


def _teardown(row, settings: Optional[Settings] = None) -> None:
    if row.get("mode") == "docker":
        _docker_rm(row.get("container_id"))
    elif row.get("mode") == "rootfs":
        sandbox_id = row.get("sandbox_task_id")
        if settings and sandbox_id:
            # Explicitly stop/release the retained rootfs when the preview is stopped
            # or expires. Both operations are best-effort and bounded by service TTL.
            base = (settings.sandbox_agent_url or "").rstrip("/")
            headers = ({"X-Sandbox-Token": settings.sandbox_token}
                       if settings.sandbox_token else {})
            try:
                import httpx
                with httpx.Client(timeout=15.0) as client:
                    client.delete(
                        f"{base}/v1/sandbox/tasks/{sandbox_id}/preview", headers=headers
                    )
            except Exception:  # noqa: BLE001
                pass
            MicroVMProvider(settings).release(sandbox_id)
    else:
        _kill(row.get("pid"))


def stop_preview(engine, preview_id: str, settings: Optional[Settings] = None) -> dict:
    with begin(engine) as c:
        row = c.execute(text(
            "SELECT pid,mode,container_id,sandbox_task_id FROM app_previews WHERE id=:id"
        ),
                        {"id": preview_id}).mappings().first()
        if row is None:
            raise ValueError("preview not found")
        _teardown(dict(row), settings)
        c.execute(text("UPDATE app_previews SET status='stopped' WHERE id=:id"), {"id": preview_id})
    return {"id": preview_id, "status": "stopped"}


def _attempt_chain(mode: str):
    """Which substrates to try, in order. `auto` prefers isolation and falls back."""
    if mode == "process":
        return [start_preview]
    if mode == "docker":
        return [start_container_preview]
    if mode == "rootfs":
        return [start_rootfs_preview]
    return [start_rootfs_preview, start_container_preview, start_preview]


#: What each substrate is called when it is being tried, so the ledger reads as English.
_SUBSTRATE_LABELS = {
    "start_rootfs_preview": "Reuse the build's retained rootfs",
    "start_container_preview": "Build and run a container",
    "start_preview": "Run as a managed host process",
}


def run_preview_attempts(engine, settings: Settings, run_id: str, *,
                         ttl_minutes: int = 60, mode: str = "auto",
                         steps=None) -> dict:
    """Try each substrate until one serves. Raises the domain errors unchanged.

    `steps` is an optional StepRecorder. `auto` tries three substrates in order, and when it
    ended in "preview could not start" there was no way to tell whether that meant one thing
    was tried and failed or three were — so each attempt records itself, with the reason it
    was passed over.
    """
    last: Optional[dict] = None
    for fn in _attempt_chain(mode):
        name = getattr(fn, "__name__", "attempt")
        label = _SUBSTRATE_LABELS.get(name, name)
        if steps is None:
            try:
                res = fn(engine, settings, run_id, ttl_minutes=ttl_minutes)
            except RuntimeError as exc:
                last = {"status": "failed", "error": str(exc)}
                continue
            if res.get("status") == "running":
                return res
            last = res
            continue
        with steps.step(name, label=label) as st:
            try:
                res = fn(engine, settings, run_id, ttl_minutes=ttl_minutes)
            except RuntimeError as exc:
                st.failed(str(exc)[:400])
                last = {"status": "failed", "error": str(exc)}
                continue
            if res.get("status") == "running":
                st.log(f"serving at {res.get('url') or 'a local URL'}")
                return res
            # Not a crash — this substrate simply could not serve, and the NEXT one may. That
            # is a skip with a reason, not a failure.
            st.skip((res.get("error") or res.get("failure_kind") or "did not serve")[:400])
            last = res
    return last or {"status": "failed", "error": "preview could not start"}


def start_preview_async(engine, settings: Settings, run_id: str, *,
                        ttl_minutes: int = 60, mode: str = "auto",
                        actor: str = "operator") -> dict:
    """Begin a preview and RETURN, leaving the build to finish in the background.

    Starting a preview clones, installs dependencies, builds and launches — minutes of work,
    and it was done inside the request. The console gives up after 120s, so the operator was
    shown "the control plane did not answer within 120s" under a heading reading "Preview /
    tests failed", while the build was still running and would often go on to succeed. Worse,
    until the attempt finished there was NO ROW AT ALL: nothing in the previews list, nothing
    in the Activity Log, nothing to poll. The operator was blind by construction.

    So a row exists from the first moment, in the `starting` state the schema already
    documented and nothing used. The console polls previews already; now there is something to
    poll. Exactly one row per attempt: whatever the substrate creates is folded back onto this
    id and its own row removed, so the id handed out here is the id that ends up terminal.
    """
    with begin(engine) as c:
        run = c.execute(text(
            "SELECT project_id, pull_request_url FROM code_task_runs WHERE id=:r"),
            {"r": run_id}).mappings().first()
    if not run:
        raise ValueError(f"code task run not found: {run_id}")

    preview_id = make_id("prev")
    project_id = run["project_id"]
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO app_previews (id,project_id,run_id,status,mode) "
            "VALUES (:id,:p,:r,'starting',:m)"),
            {"id": preview_id, "p": project_id, "r": run_id,
             "m": mode if mode in ("process", "docker", "rootfs") else "process"})
        emit_event(c, project_id, "app_preview.starting", actor, "app_preview", preview_id,
                   {"run_id": run_id, "mode": mode, "ttl_minutes": ttl_minutes})

    def _finish() -> None:
        from .deployment_steps import StepRecorder

        steps = StepRecorder(engine, preview_id, table="app_previews", set_duration=False)
        try:
            res = run_preview_attempts(engine, settings, run_id,
                                       ttl_minutes=ttl_minutes, mode=mode, steps=steps)
            child = res.get("id")
            with begin(engine) as c:
                if child and child != preview_id:
                    # Fold the substrate's row onto the id already handed out, then drop it, so
                    # a caller polling that id sees the outcome instead of a row stuck at
                    # 'starting' beside a second row it never heard about.
                    c.execute(text(
                        "UPDATE app_previews AS t SET status=s.status, url=s.url, port=s.port, "
                        "pid=s.pid, command=s.command, repo=s.repo, branch=s.branch, "
                        "mode=s.mode, substrate=s.substrate, build_substrate=s.build_substrate, "
                        "container_id=s.container_id, sandbox_task_id=s.sandbox_task_id, "
                        "error=s.error, failure_kind=s.failure_kind, log_path=s.log_path, "
                        "expires_at=s.expires_at "
                        "FROM app_previews AS s WHERE t.id=:tid AND s.id=:sid"),
                        {"tid": preview_id, "sid": child})
                    c.execute(text("DELETE FROM app_previews WHERE id=:sid"), {"sid": child})
                else:
                    status = res.get("status") or "failed"
                    error = (res.get("error") or "")[:500] or None
                    c.execute(text(
                        "UPDATE app_previews SET status=:st, error=:e WHERE id=:id"),
                        {"st": status, "e": error, "id": preview_id})
                    # A FAILURE THAT IS RETURNED IS STILL A FAILURE.
                    #
                    # The exception path below emits app_preview.failed; this one — the
                    # ordinary return carrying status='failed' — emitted nothing. That is the
                    # path a build error takes: "port_timeout: app did not serve HTTP within
                    # timeout … Could not resolve entry module index.html" was written to the
                    # preview row and to no event, so an operator who went to the Activity log
                    # to find out why found an empty screen and concluded the error had been
                    # swallowed. It had — by the log, not by the preview.
                    if status == "failed":
                        emit_event(c, project_id, "app_preview.failed", actor, "app_preview",
                                   preview_id,
                                   {"run_id": run_id,
                                    "failure_kind": res.get("failure_kind") or "start_failed",
                                    "error": error or ""})
        except Exception as exc:  # noqa: BLE001 — the thread must never die silently
            kind = "ambiguous_app" if isinstance(exc, AmbiguousAppError) else (
                "not_ready" if isinstance(exc, PreviewNotReady) else "start_crashed")
            try:
                with begin(engine) as c:
                    c.execute(text(
                        "UPDATE app_previews SET status='failed', error=:e, failure_kind=:k "
                        "WHERE id=:id"),
                        {"e": str(exc)[:500], "k": kind, "id": preview_id})
                    emit_event(c, project_id, "app_preview.failed", actor, "app_preview",
                               preview_id, {"run_id": run_id, "failure_kind": kind,
                                            "error": str(exc)[:500]})
            except Exception:  # noqa: BLE001
                logger.exception("preview %s failed and could not be recorded", preview_id)

        # THE APP'S OWN OUTPUT, not only BOSS's record of it.
        #
        # Everything above writes what BOSS observed: a status, a failure_kind, a 500-char
        # error. The container's actual log — the stack trace, the missing module, the port it
        # tried to bind — stayed on the preview host behind `read_preview_log`, reachable by an
        # agent with a prev_ id and by no one reading the Activity log. This copies the tail
        # into app_logs so it lands on that screen next to the step that caused it.
        #
        # Outside the try/except above ON PURPOSE: it must run whether the preview succeeded or
        # crashed, and a failed build is exactly the case whose log is worth having.
        try:
            from .preview_log_ingest import ingest_preview_log

            ingest_preview_log(engine, preview_id, project_id=project_id, run_id=run_id)
        except Exception:  # noqa: BLE001 — never let log capture break the preview
            logger.debug("preview %s: log ingest failed", preview_id, exc_info=True)

    threading.Thread(target=_finish, name=f"preview-{preview_id}", daemon=True).start()
    return {"id": preview_id, "status": "starting", "run_id": run_id,
            "message": "Preview is building. It appears in the previews list as it progresses."}


def list_previews(
    engine, project_id: str, limit: int = 50, settings: Optional[Settings] = None
) -> list[dict]:
    """Reap expired previews (tear down, mark, and eventually forget) and return recent ones."""
    with begin(engine) as c:
        expired = c.execute(text(
            "SELECT id,pid,mode,container_id,sandbox_task_id FROM app_previews "
            "WHERE status='running' AND expires_at < now()")).mappings().all()
        for r in expired:
            _teardown(dict(r), settings)
            c.execute(text("UPDATE app_previews SET status='expired' WHERE id=:id"), {"id": r["id"]})

        # AND THEN FORGET THE OLD ONES.
        #
        # Expiring marked rows but never removed them, so `app_previews` only grew. Measured on
        # a live deployment: 50 rows, of which 47 expired, 1 failed, 1 stopped and 1 running.
        #
        # That is not merely untidy. This query returns the newest N rows whatever their status
        # and callers filter for live ones afterwards, so a window that is 94% dead can push a
        # genuinely running preview for an older app out of it entirely — the preview exists,
        # and the app's card cannot see it.
        #
        # Seven days, and only for previews that are already finished. A preview's life is an
        # hour; a week-old expired row has no reader. The run history it belonged to lives in
        # code_task_runs and is untouched.
        c.execute(text(
            "DELETE FROM app_previews "
            "WHERE status IN ('expired','stopped','failed') "
            "  AND COALESCE(expires_at, created_at) < now() - interval '7 days'"))

        rows = c.execute(text(
            "SELECT id,run_id,repo,branch,url,port,mode,substrate,build_substrate,sandbox_task_id,"
            "status,error,failure_kind,expires_at,created_at,steps "
            "FROM app_previews WHERE project_id=:p ORDER BY created_at DESC LIMIT :l"),
            {"p": project_id, "l": int(limit)}).mappings().all()
    # Publicize at the boundary: DB keeps loopback for the proxy; callers get open_url.
    return [publicize_preview(dict(r), settings=settings) for r in rows]


def start_preview_for_run(engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60) -> dict:
    """Start a preview the same way the console's button does: try the substrates in order.

    Exists so the pipeline's auto-preview and the API endpoint share ONE launch policy. The
    endpoint used to own the ordering inline, and anything else calling a single start_* would
    have silently used a different strategy — the same drift that made the container path pick
    a different app from the host path.
    """
    attempts = (start_rootfs_preview, start_container_preview, start_preview)
    last: dict = {"status": "failed", "error": "preview could not start"}
    for fn in attempts:
        try:
            res = fn(engine, settings, run_id, ttl_minutes=ttl_minutes)
        except (PreviewNotReady, AmbiguousAppError):
            raise
        except (ValueError, RuntimeError) as exc:
            last = {"status": "failed", "error": str(exc)}
            continue
        if res.get("status") == "running":
            return res
        last = res
    return last


# --------------------------------------------------------------------------- #
# Reclaiming preview workdirs
# --------------------------------------------------------------------------- #
#: Terminal preview states. A preview in one of these can never serve again, so its tree is
#: dead weight — but note `running` is deliberately absent: that process is serving FROM its
#: workdir and deleting it pulls the floor out from under a live preview.
_TERMINAL_PREVIEW_STATES = ("failed", "stopped", "expired")

#: How long an unrecognised directory must sit before it is reclaimed. A workdir is created
#: BEFORE its row is inserted, so a young orphan may simply be a preview mid-start.
ORPHAN_WORKDIR_AFTER_HOURS = 6


def _preview_log_archive(preview_id: str) -> Path:
    """Where a reclaimed preview's log is kept.

    The log lives INSIDE the workdir (`workdir/_preview.log`), so deleting the tree destroys the
    evidence — and that log is exactly what identified `MODULE_NOT_FOUND` three levels down when
    a finished app could not be previewed. Keep the log, drop the 300 MB of node_modules around
    it: the diagnosis is a few KB, the waste is everything else.
    """
    return PREVIEW_ROOT / "_logs" / f"{preview_id}.log"


def reclaim_preview_workdir(preview_id: str) -> int:
    """Delete a terminal preview's tree, preserving its log. Returns bytes reclaimed.

    Never raises: reclaiming space must not be able to fail a caller that was doing something
    else, and a directory that has already gone is a success, not an error.
    """
    freed = 0
    try:
        if not PREVIEW_ROOT.exists():
            return 0
        archive = _preview_log_archive(preview_id)
        for d in PREVIEW_ROOT.glob(f"{preview_id}-*"):
            if not d.is_dir():
                continue
            try:
                log = d / "_preview.log"
                if log.is_file() and not archive.exists():
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(log, archive)
            except Exception:  # noqa: BLE001
                pass  # a lost log is regrettable; a full disk is an outage
            try:
                for root, _dirs, files in os.walk(d):
                    for f in files:
                        try:
                            freed += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
            except Exception:  # noqa: BLE001
                pass
            shutil.rmtree(d, ignore_errors=True)
    except Exception:  # noqa: BLE001
        logger.debug("reclaim_preview_workdir_failed preview=%s", preview_id, exc_info=True)
    return freed


def sweep_preview_workdirs(engine, *, dry_run: bool = False) -> dict:
    """Reclaim the tree of every preview that can no longer serve.

    THE SWEEP IS THE MECHANISM, not a backstop. `start_preview` has several launch paths, each
    with its own `_fail` closure, and cleanup wired into call sites is precisely how the same
    rule ends up expressed in four places and satisfied in three (S3). This joins the FILESYSTEM
    against the DATABASE, so a path that forgets to call anything is still covered — including
    one that does not exist yet.

    Two populations:
      * previews in a terminal state that still have a tree;
      * directories with no row at all — a process that died between mkdtemp and INSERT. Those
        are reclaimed only after a grace period, because a workdir is created BEFORE its row.
    """
    import time as _time

    if not PREVIEW_ROOT.exists():
        return {"reclaimed": 0, "bytes": 0, "orphans": 0, "dry_run": dry_run}

    known: dict[str, str] = {}
    try:
        with begin(engine) as c:
            for r in c.execute(text("SELECT id, status FROM app_previews")).mappings():
                known[str(r["id"])] = str(r["status"] or "")
    except Exception:  # noqa: BLE001
        logger.debug("sweep_preview_workdirs_db_failed", exc_info=True)
        return {"reclaimed": 0, "bytes": 0, "orphans": 0, "error": "db unavailable"}

    reclaimed, orphans, freed = [], 0, 0
    cutoff = _time.time() - ORPHAN_WORKDIR_AFTER_HOURS * 3600
    for d in PREVIEW_ROOT.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue  # _logs is ours
        preview_id = d.name.rsplit("-", 1)[0] if "-" in d.name else d.name
        status = known.get(preview_id)
        if status is None:
            try:
                if d.stat().st_mtime > cutoff:
                    continue  # may be starting right now
            except OSError:
                continue
            orphans += 1
        elif status not in _TERMINAL_PREVIEW_STATES:
            continue  # running, or starting — its process is serving from this tree
        if not dry_run:
            freed += reclaim_preview_workdir(preview_id)
        reclaimed.append(preview_id)

    if reclaimed and not dry_run:
        logger.info("preview_workdirs_reclaimed count=%d orphans=%d mb=%.1f",
                    len(reclaimed), orphans, freed / 1_000_000)
    return {"reclaimed": len(reclaimed), "bytes": freed, "orphans": orphans,
            "ids": reclaimed[:10], "dry_run": dry_run}
