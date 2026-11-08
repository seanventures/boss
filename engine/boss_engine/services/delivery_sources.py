"""Which repositories BOSS knows about, and what you can deploy from them.

The onboarding form asked an operator to type a repo name from memory, and silently prepended
an owner from configuration. BOSS already knows every repository it has built in — each code
task records its owner and repo, and every deployment target names one — so asking is both
unnecessary and a chance to typo a name that then fails much later at clone time.

Two reads, deliberately separate:

  * `known_repos` is derived from BOSS's OWN records. It never calls GitHub, so it works
    offline, costs nothing, and cannot be rate-limited. It answers "where have we worked?".
  * `repo_refs` asks the host what branches exist, because only the host knows. It degrades to
    the refs BOSS has seen rather than failing: a picker that shows nothing when the network
    hiccups is worse than one showing a slightly stale list, as long as it says which it is.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from ..config import Settings
from ..ports import sql


def known_repos(connection, settings: Settings,
                project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Every repository BOSS has built in or deployed from, newest activity first."""
    from .repo_links import host_of, repo_url

    default_owner = getattr(settings, "target_github_owner", "") or ""
    seen: dict[tuple[str, str], dict[str, Any]] = {}

    def add(owner: str, repo: str, source: str, when=None) -> None:
        if not repo:
            return
        key = (owner or default_owner, repo)
        entry = seen.setdefault(key, {
            "owner": key[0], "repo": key[1], "full_name": f"{key[0]}/{key[1]}",
            # None for a repository BOSS keeps on this machine (local_<project>/…): no web page.
            "url": repo_url(f"{key[0]}/{key[1]}"),
            "host": host_of(f"{key[0]}/{key[1]}"),
            "sources": [], "code_tasks": 0, "targets": 0, "last_seen": None,
        })
        if source not in entry["sources"]:
            entry["sources"].append(source)
        if source == "code_task":
            entry["code_tasks"] += 1
        elif source == "deployment_target":
            entry["targets"] += 1
        if when and (entry["last_seen"] is None or when > entry["last_seen"]):
            entry["last_seen"] = when

    # Code tasks: the richest source, since every run records the repo it worked in.
    rows = connection.execute(
        sql("SELECT spec, created_at FROM code_task_runs"
            + (" WHERE project_id = :p" if project_id else "")
            + " ORDER BY created_at DESC LIMIT 500"),
        ({"p": project_id} if project_id else {})).mappings().all()
    for r in rows:
        spec = r["spec"] or {}
        add(spec.get("owner") or "", spec.get("repo") or "", "code_task", r["created_at"])

    # Deployment targets: a repo may be registered for delivery before any task has run in it.
    trows = connection.execute(
        sql("SELECT repo, required_resources, created_at FROM deployment_targets"
            + (" WHERE project_id = :p" if project_id else "")),
        ({"p": project_id} if project_id else {})).mappings().all()
    for r in trows:
        owner = ""
        for res in r["required_resources"] or []:
            if isinstance(res, dict) and res.get("kind") == "repo_owner":
                owner = str(res.get("name") or "")
        add(owner, r["repo"] or "", "deployment_target", r["created_at"])

    # The configured default, so a fresh install offers something rather than an empty list.
    add(default_owner, getattr(settings, "target_github_repo", "") or "", "configured")

    out = sorted(seen.values(),
                 key=lambda e: (e["last_seen"] is not None, e["last_seen"] or 0), reverse=True)
    for e in out:
        e["last_seen"] = e["last_seen"].isoformat() if e["last_seen"] else None
    return out


def _ssl_context() -> Optional[ssl.SSLContext]:
    """A context with a real CA bundle.

    The python.org framework build ships no system trust store, so every HTTPS call fails with
    CERTIFICATE_VERIFY_FAILED and the branch picker silently degrades to BOSS's own records
    forever. certifi is already a dependency; using it makes the difference between a feature
    that works and one that is permanently in fallback. Verification is never disabled — a
    branch list is not worth trusting an unverified host for.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — fall back to the default context, not to no context
        return None


def _github(path: str, token: str) -> Any:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Accept": "application/vnd.github+json",
                 **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as resp:
        return json.loads(resp.read().decode() or "[]")


def repo_refs(connection, settings: Settings, owner: str, repo: str, *,
              project_id: str) -> dict[str, Any]:
    """Branches you can deploy, and the default one.

    Asks GitHub, because only the host knows what exists. On failure it falls back to refs BOSS
    has seen in its own code tasks and deployments and SAYS SO — a picker that empties itself
    when the network hiccups sends the operator back to typing branch names from memory, which
    is the problem this replaces.
    """
    token = getattr(settings, "github_token", "") or ""
    host = (getattr(settings, "target_repo_host", "github") or "github").lower()
    default_branch, branches, source, error = "main", [], host, None

    if host != "github":
        # BOSS supports other repo hosts. Rather than pretend a GitHub call will work, say so
        # and use what BOSS itself has recorded — which is host-agnostic by construction.
        source, error = "boss_records", f"branch listing not implemented for host {host!r}"
    else:
        try:
            meta = _github(f"/repos/{owner}/{repo}", token)
            default_branch = meta.get("default_branch") or "main"
            # PAGINATE. GitHub caps a page at 100 and this repo has more than that, so the
            # single-page version returned 100 machine-generated `boss/code/*` branches and
            # omitted `main` — the one branch an operator would actually pick.
            for page in range(1, 6):
                batch = _github(
                    f"/repos/{owner}/{repo}/branches?per_page=100&page={page}", token)
                if not batch:
                    break
                branches.extend({"name": b.get("name"),
                                 "sha": (b.get("commit") or {}).get("sha", "")[:7]}
                                for b in batch)
                if len(batch) < 100:
                    break
        except urllib.error.HTTPError as exc:
            source, error = "boss_records", f"github returned {exc.code}"
        except Exception as exc:  # noqa: BLE001
            source, error = "boss_records", f"github unreachable: {type(exc).__name__}"

    # What BOSS itself has shipped from this repo — always useful, and the whole answer when
    # GitHub is unavailable.
    known = connection.execute(
        sql("SELECT DISTINCT commit_ref FROM deployment_runs "
            "WHERE project_id = :p AND repo IN (:repo, :full_repo) "
            "AND commit_ref IS NOT NULL AND status = 'succeeded' "
            "ORDER BY commit_ref LIMIT 20"),
        {"p": project_id, "repo": repo, "full_repo": f"{owner}/{repo}"},
    ).mappings().all()
    deployed = [r["commit_ref"] for r in known if r["commit_ref"]]

    if not branches:
        task_branches = connection.execute(
            sql("SELECT DISTINCT finalize_ctx->>'work_branch' AS b FROM code_task_runs "
                "WHERE project_id = :p AND spec->>'repo' = :repo "
                "AND finalize_ctx->>'work_branch' IS NOT NULL "
                "ORDER BY 1 DESC LIMIT 25"),
            {"p": project_id, "repo": repo},
        ).mappings().all()
        branches = [{"name": r["b"], "sha": ""} for r in task_branches if r["b"]]
        # The default branch is almost certainly still the right first choice.
        if not any(b["name"] == default_branch for b in branches):
            branches.insert(0, {"name": default_branch, "sha": ""})

    # Order for a HUMAN picking one, not alphabetically. The default branch is what an operator
    # wants nine times in ten; `boss/code/*` work branches are machine-generated, mostly stale,
    # and there are a hundred of them — burying the useful entries under them makes the picker
    # worse than a text box.
    # The default branch must ALWAYS be offered, even when pagination or an outage means we
    # never saw it listed. It is the correct choice in almost every case, and a picker missing
    # it is a picker that cannot express the common intent.
    if default_branch and not any(b["name"] == default_branch for b in branches):
        branches.insert(0, {"name": default_branch, "sha": ""})

    deployed_set = set(deployed)

    def rank(b: dict) -> tuple:
        name = b.get("name") or ""
        return (name != default_branch,            # default branch first
                name not in deployed_set,          # then anything already shipped from here
                name.startswith("boss/"),          # then human branches, machine ones last
                name)
    branches.sort(key=rank)

    # Capped: a hundred machine branches in a dropdown is worse than a text box. The ordering
    # above guarantees the useful ones survive the cut, and `truncated` tells the console to
    # keep offering a free-text ref.
    truncated = len(branches) > 40
    branches = branches[:40]

    return {
        "owner": owner, "repo": repo, "default_branch": default_branch,
        "branches": branches, "previously_deployed": deployed,
        "truncated": truncated, "source": source, "error": error,
    }


# Entrypoints that look like a server, mirroring preview_service._PY_ENTRYPOINTS. Kept in step
# with the resolver deliberately: a picker offering an option the resolver would reject is worse
# than no picker.
_PY_HINTS = ("serve", "server", "app", "main", "wsgi")


def repo_apps(settings: Settings, owner: str, repo: str, ref: str = "") -> dict[str, Any]:
    """Which apps a repo contains, so `app entry` is a CHOICE rather than a guess.

    The resolver already computes exactly this list — it is what the ambiguity error prints —
    but only after a deploy has been attempted. An operator was expected to know the answer up
    front, and typing a package name (`nqueens_core`) instead of an entrypoint fails a minute
    later with a wall of candidates. Offering them beforehand costs one API call.

    Uses the host's contents API rather than a clone: this runs while someone waits on a form.
    """
    token = getattr(settings, "github_token", "") or ""
    host = (getattr(settings, "target_repo_host", "github") or "github").lower()
    if host != "github":
        return {"apps": [], "error": f"app discovery not implemented for host {host!r}"}

    suffix = f"?ref={urllib.parse.quote(ref)}" if ref else ""
    try:
        entries = _github(f"/repos/{owner}/{repo}/contents{suffix}", token)
    except Exception as exc:  # noqa: BLE001
        return {"apps": [], "error": f"could not list {owner}/{repo}: {type(exc).__name__}"}

    apps: list[dict[str, str]] = []
    dirs = []
    for e in entries if isinstance(entries, list) else []:
        name, kind = e.get("name", ""), e.get("type")
        if kind == "file" and name.endswith(".py") and name.startswith(_PY_HINTS):
            apps.append({"entry": name, "kind": "py",
                         "label": f"{name} — python server"})
        elif kind == "dir" and not name.startswith((".", "_")):
            dirs.append(name)

    # A directory is an app only if it can actually be served, i.e. it has an index.html.
    # Listing every directory would offer `nqueens_core` — precisely the wrong answer someone
    # already reached on their own.
    for d in dirs:
        try:
            inner = _github(
                f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(d)}{suffix}", token)
        except Exception:  # noqa: BLE001
            continue
        if any(f.get("name") == "index.html" for f in inner if isinstance(f, dict)):
            apps.append({"entry": d, "kind": "static", "label": f"{d}/ — static site"})

    apps.sort(key=lambda a: (a["kind"] != "py", a["entry"]))
    return {"owner": owner, "repo": repo, "ref": ref or "default",
            "apps": apps, "error": None,
            # Only a repo with several needs the field filled at all.
            "ambiguous": len(apps) > 1}
