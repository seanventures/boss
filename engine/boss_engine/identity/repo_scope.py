"""What a run may reach in git, for EVERY provider: one check, used at run start and at egress.

A repository a job payload or a deployment's metadata names is a REQUEST, never authority. Before
a run reaches it, the same three layers decide for GitHub and Bitbucket alike:

  1. THE REGISTRY — the repositories the install lets runs reach at all:
       GitHub:    multirepo/registry.mint_allowed (unticked, archived or unreachable: refused);
       Bitbucket: the install's registered Bitbucket repositories — BOSS_TARGET_BITBUCKET_WORKSPACE
                  / BOSS_TARGET_BITBUCKET_REPO and BOSS_BITBUCKET_REPOS (comma separated
                  `workspace/repo_slug`). FAIL CLOSED when none is registered: Bitbucket cannot
                  mint a per-run token, and the install-wide credential reaches every repository
                  of the workspace, so nothing but this list stands between a payload and them.
  2. THE CHANGE SET'S GRANT (multirepo/grants.outside_run_grant) — a build of a multi-repo app
     reaches only its app's repositories;
  3. THE PERSON'S GRANT, for a run on someone's behalf (obo) — `resources.github.repos` or
     `resources.bitbucket.repos`.

`refusal` names the first layer a set of repositories fails (the reason GitHub's mint raises);
`granted` keeps only the repositories that pass every layer, one by one — the run's git scope.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Mapping, Optional

GITHUB = "github"
BITBUCKET = "bitbucket"
PROVIDERS = (GITHUB, BITBUCKET)


def _norm(repo: str) -> str:
    return str(repo).strip().strip("/").removesuffix(".git").lower()


def bitbucket_registry() -> frozenset[str]:
    """The install's registered Bitbucket repositories (`workspace/repo_slug`, lower-cased)."""
    out: set[str] = set()
    ws = (os.environ.get("BOSS_TARGET_BITBUCKET_WORKSPACE") or "").strip()
    slug = (os.environ.get("BOSS_TARGET_BITBUCKET_REPO") or "").strip()
    if ws and slug:
        out.add(_norm(f"{ws}/{slug}"))
    for item in (os.environ.get("BOSS_BITBUCKET_REPOS") or "").split(","):
        if "/" in item and item.strip():
            out.add(_norm(item))
    return frozenset(out)


def _bitbucket_grant(grant: Any) -> frozenset[str]:
    block = ((getattr(grant, "resources", None) or {}).get(BITBUCKET)) if grant is not None else None
    repos = block.get("repos") if isinstance(block, Mapping) else None
    return frozenset(_norm(r) for r in (repos or ()) if isinstance(r, str))


def refusal(db: Any, *, provider: str, org_id: Optional[str], run_id: str, grant: Any, obo: bool,
            repos: Iterable[str]) -> Optional[str]:
    """None when the run may reach every one of `repos`; else the reason of the first layer that
    refuses: `repo_not_allowed` (registry), `repo_outside_app` (change set), `repo_outside_grant`."""
    if provider not in PROVIDERS:
        return "git_host_not_routed"
    repos = [str(r) for r in repos]
    if not repos:
        return None
    from ..multirepo import grants as repo_grants

    if provider == GITHUB:
        from ..multirepo import registry as repo_registry

        if repo_registry.mint_allowed(db, org_id, repos):
            return "repo_not_allowed"
    else:
        registered = bitbucket_registry()
        if any(_norm(r) not in registered for r in repos):
            return "repo_not_allowed"
    if repo_grants.outside_run_grant(db, run_id, repos):
        return "repo_outside_app"
    if obo:
        if provider == GITHUB:
            from . import grants

            # exactly as the GitHub mint always judged it: the grant's GitHub block, verbatim
            if not set(repos) <= set(grants.github_resources(grant).get("repos") or ()):
                return "repo_outside_grant"
        elif any(_norm(r) not in _bitbucket_grant(grant) for r in repos):
            return "repo_outside_grant"
    return None


def granted(db: Any, *, provider: str, org_id: Optional[str], run_id: str, grant: Any, obo: bool,
            repos: Iterable[str]) -> tuple[str, ...]:
    """The repositories of `repos` the run may reach — each judged alone by `refusal`. A refused
    one is simply not in the run's scope, so every call to it is `repo_outside_run`."""
    # Bitbucket names are stored NORMALISED (BOSSAPP-1185): the registry and grants compare `_norm`
    # forms, and egress compares the token's names exactly, so `acme/API.git/` must enter the
    # token as `acme/api` or a repo the mint allowed is refused at egress. GitHub keeps its names
    # verbatim (its obo check has always been exact).
    names = (_norm(r) for r in repos) if provider == BITBUCKET else (str(r) for r in repos)
    out = []
    for repo in dict.fromkeys(names):
        try:
            ok = refusal(db, provider=provider, org_id=org_id, run_id=run_id, grant=grant, obo=obo,
                         repos=[repo]) is None
        except Exception:  # noqa: BLE001 — unreadable authority: fail closed
            ok = False
        if ok:
            out.append(repo)
    return tuple(out)
