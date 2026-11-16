"""Skills from outside the project — GitHub, skills.sh, a URL, a global skill folder — pinned
and synced (BOSSAPP-841, folder import BOSSAPP-840).

A team's best playbooks live in repositories (a SKILL.md beside its scripts) and in directories
like skills.sh, which lists skills that are themselves GitHub repositories. They also live in
the operator's global Codex and Claude skill folders. Copy-pasting one in loses where it came
from and every fix made upstream afterwards. A SOURCE keeps the link:

* ``kind`` — ``github`` (``owner/repo[/path]``, a ref), ``skillsh`` (``owner/repo/skill``,
  fetched from its GitHub repository), ``url`` (a plain https URL to one SKILL.md), ``codex``
  or ``claude`` (a skill folder under that tool's global skills directory).
* ``pinned_sha`` — what the skill holds: the commit for GitHub, sha256 of the body for a URL,
  sha256 of the folder (SKILL.md plus the files beside it) for a Codex or Claude folder.
* ``auto_update`` — whether an upstream change may apply by itself.

FETCHING GOES THROUGH BOSS'S OWN CLONE PATH. A GitHub fetch is ``identity.runs.repo_access``
(the project's GitHub connection, or the egress proxy when on — the credential rides in git's
environment only, never in a URL or argv) exactly as the knowledge graph's repository index
fetches (knowledge/repo_index._checkout). No token is read here.

SYNC NEVER OVERWRITES. An upstream change becomes a NEW skill version (skill_versions) — written
by itself only when ``auto_update`` is on AND the skill is trusted (a person reviewed it);
otherwise parked as a diff-bound change_consent (BOSSAPP-843) a person approves, after which the
pin moves to the new commit. A pinned source whose pinned commit no longer resolves to the same
content records ``last_error`` and changes nothing.

The sync worker (:func:`start`) is one of the API's system threads (services/system_threads.py).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

_log = logging.getLogger("boss.skill_sources")

KINDS = ("github", "url", "skillsh", "codex", "claude")
#: A skill folder those tools already scan for the operator, not a checkout and not the network.
FOLDER_KINDS = ("codex", "claude")
#: Pinned by a content hash. A later fetch of the same bytes is the same pin; there is no commit
#: to re-resolve the way a GitHub pin is re-resolved.
_CONTENT_PINNED = frozenset({"url", *FOLDER_KINDS})
SYNC_ACTOR = "skill_sync"
MAX_BODY_BYTES = 200_000
MAX_SUPPORT_FILES = 20
MAX_SUPPORT_FILE_BYTES = 50_000
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_GH_PART = re.compile(r"^[A-Za-z0-9_.-]+$")


class SkillSourceError(ValueError):
    status = 422


@dataclass
class Fetched:
    """One fetch: the revision it came from, the SKILL.md text and its supporting files."""
    sha: str
    text: str
    filename: str = "SKILL.md"
    files: dict[str, str] = field(default_factory=dict)
    #: The folder's own name, used when a Claude SKILL.md leaves `name` out.
    folder_name: str = ""


@dataclass(frozen=True)
class Locator:
    kind: str
    owner: str = ""
    repo: str = ""
    paths: tuple[str, ...] = ()
    url: str = ""
    path: str = ""


def parse_locator(kind: str, locator: str) -> Locator:
    """Normalise what a person pasted. Accepts github.com / skills.sh URLs for their kinds."""
    k = str(kind or "").strip().lower()
    raw = str(locator or "").strip()
    if k not in KINDS:
        raise SkillSourceError(f"kind must be one of {', '.join(KINDS)}.")
    if not raw:
        raise SkillSourceError("say where the skill is.")
    if k in FOLDER_KINDS:
        if "\x00" in raw or any(part == ".." for part in Path(raw).parts):
            raise SkillSourceError("that path leaves the global skills directory.")
        return Locator(kind=k, path=raw)
    if k == "url":
        if not raw.startswith("https://"):
            raise SkillSourceError("a skill URL must start with https://.")
        return Locator(kind=k, url=raw)
    for prefix in ("https://github.com/", "https://www.github.com/", "https://skills.sh/",
                   "https://www.skills.sh/"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]
    parts = [p for p in raw.strip("/").split("/") if p]
    if k == "github" and len(parts) > 3 and parts[2] in ("tree", "blob"):
        # github.com/owner/repo/tree/<ref>/<path>: the ref is given separately, so drop it.
        parts = parts[:2] + parts[4:]
    if len(parts) < 2 or not all(_GH_PART.match(p) for p in parts):
        raise SkillSourceError("name the repository as owner/repo, optionally /path/to/skill.")
    owner, repo, rest = parts[0], parts[1], "/".join(parts[2:])
    if k == "skillsh":
        if not rest:
            return Locator(kind=k, owner=owner, repo=repo, paths=("SKILL.md",))
        return Locator(kind=k, owner=owner, repo=repo,
                       paths=(f"skills/{rest}/SKILL.md", f"{rest}/SKILL.md"))
    if rest.lower().endswith((".md", ".markdown")):
        return Locator(kind=k, owner=owner, repo=repo, paths=(rest,))
    return Locator(kind=k, owner=owner, repo=repo,
                   paths=(f"{rest}/SKILL.md" if rest else "SKILL.md",))


# ── Fetchers ──────────────────────────────────────────────────────────────────────────────────


def _git(args: list[str], *, cwd: Path, env: Optional[dict] = None, timeout: int = 300) -> Any:
    from .knowledge.repo_index import _git as repo_git

    return repo_git(args, cwd=cwd, env=env, timeout=timeout)


def fetch_github(engine: Any, settings: Any, project_id: str, loc: Locator, ref: str) -> Fetched:
    """One commit of the repository, through BOSS's clone path; the SKILL.md and its siblings."""
    from ..identity import runs as identity_runs
    from .github_connections import github_token_for

    def _token() -> str:
        with engine.begin() as c:
            return github_token_for(c, settings, project_id, loc.owner, loc.repo) or ""

    work = Path(tempfile.mkdtemp(prefix="boss-skill-src-"))
    try:
        with identity_runs.repo_access(
                engine, host=getattr(settings, "target_repo_host", "github") or "github",
                owner=loc.owner, repo=loc.repo, project_id=project_id, token=_token) as access:
            env = access.boss_environ()
            _git(["init", "--quiet"], cwd=work)
            want = ref or "HEAD"
            got = _git(["fetch", "--quiet", "--depth", "1", access.url, want], cwd=work, env=env)
            if got.returncode != 0:
                said = (got.stderr or "").strip().splitlines()[-1:] or ["git fetch failed"]
                raise SkillSourceError(f"could not fetch {loc.owner}/{loc.repo}@{want}: "
                                       f"{said[0][:200]}")
        sha = (_git(["rev-parse", "FETCH_HEAD"], cwd=work).stdout or "").strip()
        for path in loc.paths:
            shown = _git(["show", f"FETCH_HEAD:{path}"], cwd=work)
            if shown.returncode == 0:
                body = shown.stdout or ""
                if len(body.encode("utf-8")) > MAX_BODY_BYTES:
                    raise SkillSourceError(f"{path} is over {MAX_BODY_BYTES:,} bytes.")
                return Fetched(sha=sha, text=body, filename=path.rsplit("/", 1)[-1],
                               files=_siblings(work, path))
        raise SkillSourceError(
            f"{loc.owner}/{loc.repo} has no {' or '.join(loc.paths)} at {sha[:12]}.")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _siblings(work: Path, path: str) -> dict[str, str]:
    """Small text files beside the SKILL.md (its scripts and references), kept with the version."""
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    listed = _git(["ls-tree", "-r", "--long", "FETCH_HEAD", "--", folder or "."], cwd=work)
    files: dict[str, str] = {}
    for line in (listed.stdout or "").splitlines():
        meta, _, name = line.partition("\t")
        bits = meta.split()
        if len(bits) < 4 or bits[1] != "blob" or name == path:
            continue
        if not bits[3].isdigit() or int(bits[3]) > MAX_SUPPORT_FILE_BYTES:
            continue
        shown = _git(["show", f"FETCH_HEAD:{name}"], cwd=work)
        if shown.returncode == 0 and "\x00" not in (shown.stdout or ""):
            rel = name[len(folder) + 1:] if folder else name
            files[rel] = shown.stdout
        if len(files) >= MAX_SUPPORT_FILES:
            break
    return files


def fetch_url(loc: Locator, timeout: int = 20) -> Fetched:
    request = urllib.request.Request(loc.url, headers={"User-Agent": "boss-skill-sources"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 — https only
        body = resp.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        raise SkillSourceError(f"the file is over {MAX_BODY_BYTES:,} bytes.")
    sha = hashlib.sha256(body).hexdigest()
    name = loc.url.rsplit("/", 1)[-1].split("?", 1)[0] or "SKILL.md"
    if not name.lower().endswith((".md", ".markdown", ".txt")):
        name = "SKILL.md"
    return Fetched(sha=sha, text=body.decode("utf-8", errors="replace"), filename=name)


def _add_root(found: list[Path], path: Path) -> None:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return
    if resolved not in found:
        found.append(resolved)


def folder_roots(kind: str) -> list[Path]:
    """Directories the tool already scans for the operator's own skills.

    Codex: ``$CODEX_HOME/skills`` when set, otherwise ``~/.codex/skills``, plus the newer
    ``~/.agents/skills`` and the admin directory ``/etc/codex/skills``. Claude: ``~/.claude/skills``,
    or ``$CLAUDE_CONFIG_DIR/skills`` when that home is moved.
    """
    found: list[Path] = []
    if kind == "codex":
        codex_home = os.environ.get("CODEX_HOME", "").strip()
        if codex_home:
            _add_root(found, Path(codex_home) / "skills")
        _add_root(found, Path.home() / ".codex" / "skills")
        _add_root(found, Path.home() / ".agents" / "skills")
        _add_root(found, Path("/etc/codex/skills"))
    else:
        config = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        if config:
            _add_root(found, Path(config) / "skills")
        _add_root(found, Path.home() / ".claude" / "skills")
    return found


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _skill_dir(candidate: Path, roots: list[Path]) -> Optional[Path]:
    """The skill folder `candidate` names, only when it stays inside a global root."""
    try:
        if not candidate.exists():
            return None
        resolved = candidate.resolve()
    except OSError:
        return None
    if resolved.name == "SKILL.md":
        resolved = resolved.parent
    if not any(_under(resolved, root) and resolved != root for root in roots):
        return None
    skill_md = resolved / "SKILL.md"
    try:
        if not skill_md.is_file() or not _under(skill_md.resolve(), resolved):
            return None
    except OSError:
        return None
    return resolved


def _folder_files(folder: Path) -> dict[str, str]:
    """Text files beside SKILL.md. A symlink that leaves the folder is left out, not read."""
    files: dict[str, str] = {}
    root = folder.resolve()
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
        here = Path(dirpath)
        for name in sorted(filenames):
            if name.startswith(".") or (here == folder and name == "SKILL.md"):
                continue
            path = here / name
            if path.is_symlink():
                try:
                    if not _under(path.resolve(), root):
                        continue
                except OSError:
                    continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data or len(data) > MAX_SUPPORT_FILE_BYTES:
                continue
            try:
                files[path.relative_to(folder).as_posix()] = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if len(files) >= MAX_SUPPORT_FILES:
                return files
    return files


def _folder_sha(body: str, files: dict[str, str]) -> str:
    hasher = hashlib.sha256()
    hasher.update(body.encode("utf-8"))
    hasher.update(b"\0")
    for name in sorted(files):
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(files[name].encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def fetch_folder(loc: Locator) -> Fetched:
    """One global skill folder: its SKILL.md and the text files beside it. No network."""
    roots = folder_roots(loc.kind)
    raw = os.path.expanduser(loc.path.strip())
    path = Path(raw)
    candidates = [path] if path.is_absolute() else [root / path for root in roots]
    folder: Optional[Path] = None
    for candidate in candidates:
        folder = _skill_dir(candidate, roots)
        if folder is not None:
            break
    if folder is None:
        raise SkillSourceError(
            f"no {loc.kind} skill folder {loc.path!r} under the global skills directory.")
    data = (folder / "SKILL.md").read_bytes()
    if b"\x00" in data:
        raise SkillSourceError("SKILL.md is not a text file.")
    if len(data) > MAX_BODY_BYTES:
        raise SkillSourceError(f"SKILL.md is over {MAX_BODY_BYTES:,} bytes.")
    body = data.decode("utf-8")
    files = _folder_files(folder)
    return Fetched(sha=_folder_sha(body, files), text=body, filename="SKILL.md", files=files,
                   folder_name=folder.name)


Fetcher = Callable[[Locator, str], Fetched]


def default_fetcher(engine: Any, settings: Any, project_id: str) -> Fetcher:
    def fetch(loc: Locator, ref: str) -> Fetched:
        if loc.kind == "url":
            return fetch_url(loc)
        if loc.kind in FOLDER_KINDS:
            return fetch_folder(loc)
        return fetch_github(engine, settings, project_id, loc, ref)

    return fetch


# ── Preview and import ────────────────────────────────────────────────────────────────────────


def _as_skill(fetched: Fetched, skill_key: Optional[str]) -> dict[str, Any]:
    """The guide skill a SKILL.md becomes (agent_instructions.parse_skill_md: the same parser and
    lint as an uploaded file)."""
    from .agent_instructions import InstructionError, parse_frontmatter, parse_skill_md

    head, _rest = parse_frontmatter(fetched.text)
    # Claude uses the folder name when the frontmatter leaves `name` out. A name that is
    # present still wins (parse_skill_md: an explicit label overrides the file).
    label = fetched.folder_name if fetched.folder_name and not head.get("name") else None
    try:
        parsed = parse_skill_md(fetched.filename, fetched.text, label=label)
    except InstructionError as error:
        raise SkillSourceError(f"{error.field}: {error.tip or error.message}") from error
    if skill_key:
        parsed["skill_key"] = str(skill_key).strip().lower()
    return parsed


def preview(fetcher: Fetcher, *, kind: str, locator: str, ref: str = "",
            pin: str = "", skill_key: Optional[str] = None) -> dict[str, Any]:
    """What importing would create, fetched at `pin` (or `ref`), writing nothing."""
    return _fetch_for_import(fetcher, kind=kind, locator=locator, ref=ref, pin=pin,
                             skill_key=skill_key)[0]


def _fetch_for_import(fetcher: Fetcher, *, kind: str, locator: str, ref: str, pin: str,
                      skill_key: Optional[str]) -> tuple[dict[str, Any], Fetched]:
    loc = parse_locator(kind, locator)
    pinned = str(pin or "").strip()
    if pinned and loc.kind not in _CONTENT_PINNED and not _SHA.match(pinned):
        raise SkillSourceError("a pin is a commit sha (7-64 hex characters).")
    fetched = fetcher(loc, pinned if loc.kind != "url" and pinned else ref)
    if pinned and not fetched.sha.startswith(pinned) and not pinned.startswith(fetched.sha):
        raise SkillSourceError(
            f"the pin {pinned[:12]} does not match what was fetched ({fetched.sha[:12]}).")
    skill = _as_skill(fetched, skill_key)
    return ({"kind": loc.kind, "locator": locator, "ref": ref, "sha": fetched.sha,
             "skill": skill, "files": sorted(fetched.files)}, fetched)


def _source(row: Any) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(row).items()}


def list_sources(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT * FROM skill_sources WHERE project_id = :p ORDER BY skill_key"),
        {"p": project_id}).mappings().all()
    return [_source(r) for r in rows]


def import_source(connection: Connection, fetcher: Fetcher, *, project_id: str, actor: str,
                  kind: str, locator: str, ref: str = "", pin: str = "",
                  auto_update: bool = False, skill_key: Optional[str] = None,
                  org_id: Optional[str] = None, scope: str = "project") -> dict[str, Any]:
    """Create the skill (version 1, a person's act) and its source row, pinned to what was shown."""
    from .skill_definitions import save_skill

    shown, fetched = _fetch_for_import(fetcher, kind=kind, locator=locator, ref=ref, pin=pin,
                                       skill_key=skill_key)
    loc = parse_locator(kind, locator)
    fetched_files = fetched.files
    skill = shown["skill"]
    # WHERE SKILLS MAY COME FROM, AS DATA (BOSSAPP-890): checked with the source it comes from.
    from .skill_policy import check

    check(connection, project_id, action="import", skill_key=skill["skill_key"],
          source=f"{loc.kind}:{locator.strip()}", risk_tier=0, actor=actor)
    saved = save_skill(
        connection, project_id=project_id, actor=actor, skill_key=skill["skill_key"],
        label=skill["label"], behaviour_kind="guide", summary=skill["summary"],
        knowledge=skill["knowledge"], risk_tier=0, mode="create", origin="person",
        version_note=f"imported from {loc.kind}:{locator}@{shown['sha'][:12]}",
        files=fetched_files or None)
    row = connection.execute(
        text("INSERT INTO skill_sources (id, project_id, org_id, scope, kind, locator, ref, "
             "skill_key, pinned_sha, upstream_sha, auto_update, last_sync_at, created_by) "
             "VALUES (:id, :p, :o, :sc, :k, :l, :r, :key, :sha, :sha, :au, now(), :by) "
             "RETURNING *"),
        {"id": make_id("sks"), "p": project_id, "o": org_id,
         "sc": scope if scope in ("project", "org") else "project", "k": loc.kind,
         "l": locator.strip(), "r": str(ref or "").strip(), "key": saved["skill_key"],
         "sha": shown["sha"], "au": bool(auto_update), "by": actor}).mappings().one()
    emit_event(connection, project_id, "skill.source_imported", actor, "skill_source", row["id"],
               {"kind": loc.kind, "locator": locator, "sha": shown["sha"],
                "skill_key": saved["skill_key"]})
    return {"source": _source(row), "skill": saved}


def update_source(connection: Connection, *, project_id: str, actor: str, source_id: str,
                  auto_update: Optional[bool] = None, ref: Optional[str] = None) -> dict:
    row = connection.execute(
        text("UPDATE skill_sources SET auto_update = COALESCE(:au, auto_update), "
             "ref = COALESCE(:r, ref) WHERE id = :i AND project_id = :p RETURNING *"),
        {"au": auto_update, "r": ref, "i": source_id, "p": project_id}).mappings().first()
    if row is None:
        raise SkillSourceError(f"no skill source {source_id!r}.")
    emit_event(connection, project_id, "skill.source_changed", actor, "skill_source", source_id,
               {"auto_update": row["auto_update"], "ref": row["ref"]})
    return _source(row)


def mark_applied(connection: Connection, source_id: str, upstream_sha: str) -> None:
    """A parked upstream change was approved and written: the pin moves to it."""
    connection.execute(
        text("UPDATE skill_sources SET pinned_sha = COALESCE(NULLIF(:s, ''), pinned_sha), "
             "pending_consent_id = NULL, last_error = NULL WHERE id = :i"),
        {"s": upstream_sha, "i": source_id})


# ── Sync ──────────────────────────────────────────────────────────────────────────────────────


def _trusted(connection: Connection, project_id: str, skill_key: str) -> bool:
    return bool(connection.execute(
        text("SELECT trusted FROM skill_definitions WHERE project_id = :p AND skill_key = :k"),
        {"p": project_id, "k": skill_key}).scalar())


def sync_one(engine: Any, settings: Any, source_id: str, *,
             fetcher: Optional[Fetcher] = None) -> dict[str, Any]:
    """Check one source upstream. Returns {outcome: unchanged|applied|parked|error, ...}."""
    from .skill_definitions import (SkillDefinitionError, get_skill, prepare_skill,
                                    propose_change, save_skill)

    with engine.begin() as c:
        row = c.execute(text("SELECT * FROM skill_sources WHERE id = :i"),
                        {"i": source_id}).mappings().first()
    if row is None:
        raise SkillSourceError(f"no skill source {source_id!r}.")
    source = dict(row)
    project_id = source["project_id"]
    fetch = fetcher or default_fetcher(engine, settings, project_id)
    loc = parse_locator(source["kind"], source["locator"])

    def finish(outcome: str, **extra: Any) -> dict[str, Any]:
        with engine.begin() as c:
            c.execute(text("UPDATE skill_sources SET last_sync_at = now(), last_error = :e, "
                           "upstream_sha = COALESCE(:u, upstream_sha), "
                           "pending_consent_id = COALESCE(:pc, pending_consent_id) "
                           "WHERE id = :i"),
                      {"e": extra.get("error"), "u": extra.get("upstream_sha"),
                       "pc": extra.get("consent_id"), "i": source_id})
            emit_event(c, project_id, f"skill.source_{outcome}", SYNC_ACTOR, "skill_source",
                       source_id, {k: v for k, v in extra.items() if k != "files"})
        return {"source_id": source_id, "outcome": outcome, **extra}

    try:
        upstream = fetch(loc, source["ref"])
        pinned = source["pinned_sha"]
        if loc.kind not in _CONTENT_PINNED and pinned and upstream.sha != pinned:
            # VERIFY THE PIN before reasoning about a change: what the skill holds must still
            # resolve to a commit that is the pin.
            at_pin = fetch(loc, pinned)
            if at_pin.sha != pinned:
                return finish("error", error=f"the pinned commit {pinned[:12]} no longer "
                                             f"resolves (got {at_pin.sha[:12]}).")
        if upstream.sha == pinned:
            return finish("unchanged", upstream_sha=upstream.sha)
        if source["pending_consent_id"] and source["upstream_sha"] == upstream.sha:
            return finish("unchanged", upstream_sha=upstream.sha, note="already awaiting approval")
        parsed = _as_skill(upstream, source["skill_key"])
        with engine.begin() as c:
            live = get_skill(c, project_id, source["skill_key"])
            if live is None:
                return finish("error", error=f"skill {source['skill_key']!r} no longer exists.")
            # A RULE WRITTEN SINCE THE IMPORT still governs its updates (BOSSAPP-890).
            from .skill_policy import check

            check(c, project_id, action="import", skill_key=source["skill_key"],
                  source=f"{loc.kind}:{source['locator']}", risk_tier=live["risk_tier"],
                  actor=SYNC_ACTOR, actor_kind="person")
            note = f"synced from {loc.kind}:{source['locator']}@{upstream.sha[:12]}"
            if source["auto_update"] and _trusted(c, project_id, source["skill_key"]):
                save_skill(c, project_id=project_id, actor=SYNC_ACTOR,
                           skill_key=source["skill_key"], label=parsed["label"],
                           behaviour_kind=live["behaviour_kind"], summary=parsed["summary"],
                           knowledge=parsed["knowledge"], inputs=live["inputs"],
                           produces=live["produces"], config=live["config"],
                           risk_tier=live["risk_tier"], trusted=live["trusted"],
                           enabled=live["enabled"], mode="update", origin="sync",
                           version_note=note, files=upstream.files or None)
                mark_applied(c, source_id, upstream.sha)
                applied = True
            else:
                prepared = prepare_skill(
                    c, project_id=project_id, skill_key=source["skill_key"],
                    label=parsed["label"], behaviour_kind=live["behaviour_kind"],
                    summary=parsed["summary"], knowledge=parsed["knowledge"],
                    inputs=live["inputs"], produces=live["produces"], config=live["config"],
                    risk_tier=live["risk_tier"], mode="update")
                parked = propose_change(
                    c, project_id=project_id, actor=SYNC_ACTOR, prepared=prepared,
                    mode="update", reason="skill_source_update", files=upstream.files or None,
                    meta={"source_id": source_id, "upstream_sha": upstream.sha}, note=note)
                applied = False
        if applied:
            return finish("applied", upstream_sha=upstream.sha)
        return finish("parked", upstream_sha=upstream.sha, consent_id=parked["consent_id"],
                      decision_id=parked["decision_id"])
    except (SkillSourceError, SkillDefinitionError) as error:
        return finish("error", error=str(error)[:500])
    except Exception as error:  # noqa: BLE001 — a source that cannot be read is a recorded error
        _log.warning("skill source %s sync failed", source_id, exc_info=True)
        return finish("error", error=f"{type(error).__name__}: {error}"[:500])


def sync_due(engine: Any, settings: Any, *, every_minutes: int = 360, limit: int = 20,
             fetcher: Optional[Fetcher] = None) -> list[dict[str, Any]]:
    with engine.begin() as c:
        ids = c.execute(
            text("SELECT id FROM skill_sources WHERE last_sync_at IS NULL "
                 "OR last_sync_at < now() - make_interval(mins => :m) "
                 "ORDER BY last_sync_at NULLS FIRST LIMIT :l"),
            {"m": int(every_minutes), "l": int(limit)}).scalars().all()
    return [sync_one(engine, settings, sid, fetcher=fetcher) for sid in ids]


_STARTED = threading.Event()


def start(engine: Any, settings: Any) -> None:
    """The sync worker: one daemon thread, every BOSS_SKILL_SYNC_MINUTES (default 360; 0 = off)."""
    import os

    minutes = int(os.environ.get("BOSS_SKILL_SYNC_MINUTES", "360") or 0)
    if minutes <= 0 or _STARTED.is_set():
        return
    _STARTED.set()

    def loop() -> None:
        time.sleep(60)  # let the API finish booting first
        while True:
            try:
                sync_due(engine, settings, every_minutes=minutes)
            except Exception:  # noqa: BLE001 — the loop must outlive one bad tick
                _log.warning("skill source sync tick failed", exc_info=True)
            time.sleep(max(60, minutes * 60 // 6))

    threading.Thread(target=loop, name="boss-skill-sources", daemon=True).start()

