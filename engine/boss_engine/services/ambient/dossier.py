"""Discovery: the project dossier BOSS writes when it first reads a repository (BOSSAPP-909).

WHY (design §4 L1: "give it a directory and it starts working")
---------------------------------------------------------------
A new project used to open on an empty screen until a person typed what to do. Discovery reads
the repository with NO instructions — most facts without a model — and keeps one current
DOSSIER per project repository: what it is built with, how it is laid out, how to test and build
it, who owns it, what docs and runbooks it has, where TODOs cluster, which files are risky
(churn × size), what it depends on and which advisories hit it, what BOSS already has open on it
and what the knowledge graph knows. Then it SEEDS SIGNALS for what is worth doing ("no CI", "a
hot file with no tests", a vulnerable dependency): the Chief of Staff triages them like any
other signal, so the operator sees a dossier and a ranked list of proposals (A1 cards; a TODO
cluster is A0, digest only).

WHERE THE FACTS COME FROM (reuse, design §4 L1 table)
-----------------------------------------------------
  stack, build, tests       start_project.detect over the file list (the importer's own reading)
  structure, CI, commands   the file tree and the manifests (package.json scripts, Makefile …)
  owners                    CODEOWNERS, and `git shortlog` names (never emails)
  hotspots                  `git log --numstat` churn over 180 days × file size
  dependencies, advisories  lockfiles; OSV's querybatch when the network allows (5 s, offline →
                            "skipped", never an error); the security scan's OSV findings are the
                            same fingerprint, so the two never raise the same advisory twice
  open work, KG             BOSS's own tables: open BOSS PRs, live ambient items, kg_nodes
It is built from the checkout the code-graph index already keeps (repo_index), right after a
repository is indexed, and on request (POST /v1/ambient/dossier). With no checkout it reads the
graph's file list and says so (`read_from: graph`).

Everything read from the repository is DATA (design §9): bounded, cleaned, never executed.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ._common import clean_text, emit, new_id, serialize_row

log = logging.getLogger(__name__)

SKIP_DIRS = frozenset({".git", "node_modules", "vendor", "dist", "build", ".venv", "venv",
                       "__pycache__", "target", ".next", ".tox", ".mypy_cache", ".pytest_cache",
                       "coverage", ".gradle", ".idea"})
MAX_FILES = 20_000
MAX_SCAN_BYTES = 512_000          # a file larger than this is not read for TODOs / lines
CI_FILES = (".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml", "azure-pipelines.yml",
            ".travis.yml", "bitbucket-pipelines.yml", ".drone.yml", "buildkite.yml")
_MARKER = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
_SOURCE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".java", ".kt", ".go", ".rs", ".rb",
               ".php", ".cs", ".swift", ".scala", ".c", ".cc", ".cpp", ".h", ".vue", ".svelte"}
_RUNBOOK = re.compile(r"(runbook|playbook|on-?call|incident|troubleshoot)", re.I)
_ADR = re.compile(r"(^|/)(adr|adrs|decisions)/", re.I)
HOTSPOT_DAYS = 180
GIT_TIMEOUT = 30
OSV_URL = "https://api.osv.dev/v1/querybatch"
OSV_TIMEOUT = 5.0
#: Signals Discovery seeds per build, at most (a big legacy repo is not 400 cards).
MAX_SEEDED_HOTSPOTS = 3
TODO_CLUSTER_MIN = 5


# ── reading a checkout ──────────────────────────────────────────────────────────────────────────

def list_files(root: Path, limit: int = MAX_FILES) -> list[str]:
    out: list[str] = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
                         or d in (".github", ".circleci"))
        for name in sorted(files):
            rel = Path(base, name).relative_to(root).as_posix()
            out.append(rel)
            if len(out) >= limit:
                return out
    return out


def _read(root: Path, rel: str, limit: int = MAX_SCAN_BYTES) -> str:
    path = root / rel
    try:
        if path.is_symlink() or path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _git(root: Path, *args: str) -> str:
    if not (root / ".git").exists():
        return ""
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, check=False,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def structure(paths: Iterable[str]) -> list[dict[str, Any]]:
    """Top-level directories by file count (and '.' for files at the root)."""
    counts: Counter[str] = Counter()
    for p in paths:
        head = p.split("/", 1)[0] if "/" in p else "."
        counts[head] += 1
    return [{"dir": d, "files": n} for d, n in counts.most_common(12)]


def languages(paths: Iterable[str]) -> list[dict[str, Any]]:
    from ..start_project import _EXT_LANG

    counts: Counter[str] = Counter()
    for p in paths:
        lang = _EXT_LANG.get(PurePosixPath(p).suffix.lower())
        if lang:
            counts[lang] += 1
    return [{"language": k, "files": n} for k, n in counts.most_common(5)]


def ci_files(paths: Iterable[str]) -> list[str]:
    found = [p for p in paths if re.match(r"^\.github/workflows/[^/]+\.ya?ml$", p)
             or p in CI_FILES]
    return sorted(found)[:20]


def commands(root: Optional[Path], paths: list[str]) -> dict[str, list[str]]:
    """How to test, lint and build it, from what the repository itself declares."""
    have = set(paths)
    out: dict[str, list[str]] = {"test": [], "lint": [], "build": []}

    def add(kind: str, cmd: str) -> None:
        if cmd not in out[kind]:
            out[kind].append(cmd)

    if root is not None and "package.json" in have:
        try:
            scripts = (json.loads(_read(root, "package.json")) or {}).get("scripts") or {}
        except ValueError:
            scripts = {}
        for kind, keys in (("test", ("test", "test:unit")), ("lint", ("lint", "typecheck")),
                           ("build", ("build",))):
            for key in keys:
                if key in scripts:
                    add(kind, f"npm run {key}" if key != "test" else "npm test")
    if root is not None and "Makefile" in have:
        targets = set(re.findall(r"^([A-Za-z][\w-]*):", _read(root, "Makefile"), re.M))
        for kind in ("test", "lint", "build"):
            if kind in targets:
                add(kind, f"make {kind}")
    if have & {"pyproject.toml", "setup.py", "requirements.txt", "pytest.ini", "tox.ini"}:
        if any(re.search(r"(^|/)test_[^/]+\.py$|(^|/)tests?/", p) for p in paths):
            add("test", "pytest")
        if root is not None and "ruff" in _read(root, "pyproject.toml"):
            add("lint", "ruff check .")
    if "pom.xml" in have:
        add("test", "mvn test")
        add("build", "mvn package")
    if have & {"build.gradle", "build.gradle.kts"}:
        add("test", "./gradlew test")
        add("build", "./gradlew build")
    if "go.mod" in have:
        add("test", "go test ./...")
        add("build", "go build ./...")
    if "Cargo.toml" in have:
        add("test", "cargo test")
        add("build", "cargo build")
    return out


def owners(root: Optional[Path], paths: list[str]) -> dict[str, Any]:
    """CODEOWNERS rules and the people who commit most (names only — never an email)."""
    rules: list[dict[str, Any]] = []
    source = next((p for p in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")
                   if p in set(paths)), None)
    if root is not None and source:
        for line in _read(root, source).splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            pattern, *who = line.split()
            if who:
                rules.append({"pattern": clean_text(pattern, 200),
                              "owners": [clean_text(w, 80) for w in who[:5]]})
    people: list[dict[str, Any]] = []
    if root is not None:
        for line in _git(root, "shortlog", "-sn", "--no-merges", "HEAD").splitlines()[:5]:
            count, _, name = line.strip().partition("\t")
            if name:
                people.append({"name": clean_text(name, 80), "commits": int(count or 0)})
    return {"codeowners": source, "rules": rules[:20], "top_committers": people}


def docs(paths: list[str]) -> dict[str, Any]:
    md = [p for p in paths if p.lower().endswith((".md", ".rst", ".adoc"))]
    readme = next((p for p in paths if "/" not in p and p.lower().startswith("readme")), None)
    return {"readme": readme,
            "contributing": next((p for p in paths if p.lower().split("/")[-1].startswith(
                "contributing")), None),
            "docs": len(md), "sample": sorted(md, key=lambda p: (p.count("/"), p))[:8],
            "runbooks": [p for p in md if _RUNBOOK.search(p)][:8],
            "adrs": len([p for p in md if _ADR.search(p)])}


def todos(root: Optional[Path], paths: list[str]) -> dict[str, Any]:
    """TODO/FIXME/HACK/XXX markers in source, clustered by directory."""
    per_file: dict[str, int] = {}
    if root is not None:
        for p in paths:
            if PurePosixPath(p).suffix.lower() not in _SOURCE_EXT:
                continue
            n = len(_MARKER.findall(_read(root, p)))
            if n:
                per_file[p] = n
    clusters: dict[str, int] = defaultdict(int)
    for p, n in per_file.items():
        clusters[str(PurePosixPath(p).parent)] += n
    ranked = sorted(clusters.items(), key=lambda kv: -kv[1])[:5]
    return {"total": sum(per_file.values()), "files": len(per_file),
            "clusters": [{"dir": d, "count": n,
                          "files": sorted((f for f in per_file if str(PurePosixPath(f).parent)
                                           == d), key=lambda f: -per_file[f])[:3]}
                         for d, n in ranked]}


def _has_test_for(path: str, test_paths: list[str]) -> bool:
    stem = PurePosixPath(path).stem.lower()
    stem = re.sub(r"^(test_)|(_test|\.test|\.spec|test)$", "", stem)
    return bool(stem) and any(stem in PurePosixPath(t).name.lower() for t in test_paths)


def hotspots(root: Optional[Path], paths: list[str], *, days: int = HOTSPOT_DAYS) -> list[dict]:
    """Files that change most AND are big: where a bug is most likely (churn × log size)."""
    from ..start_project import _TEST_PATH

    if root is None:
        return []
    churn: Counter[str] = Counter()
    for line in _git(root, "log", f"--since={days}.days", "--numstat", "--format=",
                     "--no-merges", "-n", "2000").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            churn[parts[2]] += int(parts[0]) + int(parts[1])
    live = set(paths)
    tests = [p for p in paths if _TEST_PATH.search(p)]
    out = []
    for path, changed in churn.items():
        if path not in live or PurePosixPath(path).suffix.lower() not in _SOURCE_EXT \
                or _TEST_PATH.search(path):
            continue
        lines = _read(root, path).count("\n")
        out.append({"file": path, "churn": changed, "lines": lines,
                    "score": round(changed * math.log2(2 + lines), 1),
                    "has_tests": _has_test_for(path, tests)})
    return sorted(out, key=lambda h: -h["score"])[:10]


def dependencies(root: Optional[Path], paths: list[str]) -> dict[str, Any]:
    """Direct and locked packages from the lockfiles BOSS can read without running anything."""
    found: dict[tuple[str, str], str] = {}
    manifests = [p for p in paths if PurePosixPath(p).name in (
        "package-lock.json", "requirements.txt", "poetry.lock", "package.json", "go.mod",
        "pom.xml", "Cargo.lock", "pyproject.toml")]
    if root is not None:
        for rel in manifests:
            name = PurePosixPath(rel).name
            body = _read(root, rel, 5_000_000)
            if name == "package-lock.json":
                try:
                    data = json.loads(body or "{}")
                except ValueError:
                    data = {}
                for key, meta in (data.get("packages") or {}).items():
                    if key.startswith("node_modules/") and isinstance(meta, Mapping) \
                            and meta.get("version"):
                        found[("npm", key.rsplit("node_modules/", 1)[1])] = str(meta["version"])
            elif name == "requirements.txt" or (name.startswith("requirements") and
                                                name.endswith(".txt")):
                for line in body.splitlines():
                    m = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([\w.\-+!]+)", line)
                    if m:
                        found[("PyPI", m.group(1).lower())] = m.group(2)
            elif name == "poetry.lock":
                for m in re.finditer(r'\[\[package\]\]\s*name\s*=\s*"([^"]+)"\s*version\s*=\s*'
                                     r'"([^"]+)"', body):
                    found[("PyPI", m.group(1).lower())] = m.group(2)
    packages = [{"ecosystem": e, "name": n, "version": v} for (e, n), v in sorted(found.items())]
    return {"manifests": sorted(manifests)[:20], "count": len(packages),
            "packages": packages[:2000]}


def osv_lookup(packages: list[Mapping[str, Any]], *, timeout: float = OSV_TIMEOUT
               ) -> dict[str, Any]:
    """OSV's querybatch for the locked packages. Offline, slow or refused → status `skipped`
    with the reason; never raises. Honours the proxy environment (boss-egress)."""
    import urllib.error
    import urllib.request

    if not packages:
        return {"status": "ok", "advisories": []}
    queries = [{"package": {"ecosystem": p["ecosystem"], "name": p["name"]},
                "version": p["version"]} for p in packages[:1000]]
    try:
        req = urllib.request.Request(OSV_URL, data=json.dumps({"queries": queries}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https URL
            results = (json.loads(resp.read() or b"{}") or {}).get("results") or []
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {"status": "skipped", "reason": f"OSV not reachable ({type(exc).__name__})",
                "advisories": []}
    out = []
    for pkg, result in zip(packages, results):
        ids = [str(v.get("id")) for v in (result or {}).get("vulns") or [] if v.get("id")]
        if ids:
            out.append({**pkg, "ids": ids[:10]})
    return {"status": "ok", "advisories": out}


def scan_tree(root: Path, *, osv: Optional[Callable[[list], dict]] = None) -> dict[str, Any]:
    """Every fact Discovery reads from a checkout. No model, no network unless `osv` is given."""
    from ..start_project import _TEST_PATH, detect

    paths = list_files(root)
    deps = dependencies(root, paths)
    facts = {
        "files": len(paths), "truncated": len(paths) >= MAX_FILES,
        "stack": detect(paths), "languages": languages(paths), "structure": structure(paths),
        "ci": ci_files(paths), "commands": commands(root, paths),
        "tests": {"files": len([p for p in paths if _TEST_PATH.search(p)])},
        "owners": owners(root, paths), "docs": docs(paths), "todos": todos(root, paths),
        "hotspots": hotspots(root, paths), "dependencies": {k: v for k, v in deps.items()
                                                            if k != "packages"},
        "advisories": (osv or (lambda _p: {"status": "skipped", "reason": "not asked",
                                           "advisories": []}))(deps["packages"]),
    }
    return facts


def facts_from_paths(paths: list[str]) -> dict[str, Any]:
    """What can be said from a file list alone (no checkout): no TODOs, hotspots or owners."""
    from ..start_project import _TEST_PATH, detect

    return {"files": len(paths), "truncated": False, "stack": detect(paths),
            "languages": languages(paths), "structure": structure(paths), "ci": ci_files(paths),
            "commands": commands(None, paths),
            "tests": {"files": len([p for p in paths if _TEST_PATH.search(p)])},
            "owners": owners(None, paths), "docs": docs(paths), "todos": todos(None, paths),
            "hotspots": [], "dependencies": dependencies(None, paths),
            "advisories": {"status": "skipped", "reason": "no checkout", "advisories": []}}


# ── BOSS's own view of the project ──────────────────────────────────────────────────────────────

def boss_view(connection: Connection, project_id: str, repo: str) -> dict[str, Any]:
    """Open BOSS PRs on this repository, live ambient items, and the knowledge graph's summary."""
    prs = connection.execute(text(
        "SELECT c.pull_request_url, c.title FROM code_task_runs c JOIN decisions d ON "
        "d.id = c.decision_id WHERE c.project_id = :p AND c.pull_request_url ILIKE :r "
        "AND d.status IN ('pending', 'delegated') ORDER BY c.created_at DESC LIMIT 5"),
        {"p": project_id, "r": f"%github.com/{repo}/pull/%"}).mappings().all()
    items = int(connection.execute(text(
        "SELECT count(*) FROM work_items WHERE project_id = :p AND ambient "
        "AND lifecycle IN ('proposed', 'claimed', 'in_progress', 'needs_you')"),
        {"p": project_id}).scalar() or 0)
    kinds = connection.execute(text(
        "SELECT kind, count(*) AS n FROM kg_nodes WHERE project_id = :p AND (repo = :r OR "
        "repo IS NULL OR repo = '') AND valid_to IS NULL GROUP BY kind ORDER BY n DESC LIMIT 8"),
        {"p": project_id, "r": repo}).mappings().all()
    return {"open_boss_prs": [{"url": r["pull_request_url"], "title": r["title"]} for r in prs],
            "live_items": items,
            "kg": {"nodes": sum(int(k["n"]) for k in kinds),
                   "by_kind": {k["kind"]: int(k["n"]) for k in kinds}}}


# ── highlights and seeded signals ───────────────────────────────────────────────────────────────

def highlights(facts: Mapping[str, Any]) -> list[str]:
    """Badge-sized lines (≤ 4 words) for the console's dossier card (§11.0)."""
    stack = facts.get("stack") or {}
    words = [w for w in (stack.get("language"), stack.get("framework")) if w]
    out = [" · ".join(words) or "Stack unknown"]
    out.append(f"{len(facts.get('ci') or [])} CI workflows" if facts.get("ci") else "No CI")
    tests = int((facts.get("tests") or {}).get("files") or 0)
    out.append(f"{tests} test files" if tests else "No tests")
    todo = int((facts.get("todos") or {}).get("total") or 0)
    if todo:
        out.append(f"{todo} TODOs")
    hot = [h for h in facts.get("hotspots") or [] if not h.get("has_tests")]
    if hot:
        out.append(f"{len(hot)} untested hotspots")
    adv = (facts.get("advisories") or {}).get("advisories") or []
    if adv:
        out.append(f"{len(adv)} vulnerable packages")
    return out[:6]


def seed(facts: Mapping[str, Any], *, project_id: str, repo: str, dossier_id: str,
         head: str) -> list[dict[str, Any]]:
    """record_signal kwargs for what Discovery found worth doing (source `scan`/`osv`)."""
    out: list[dict[str, Any]] = []
    cite = {"kind": "dossier", "ref": dossier_id}
    tag = head[:12] or "nohead"

    def add(kind: str, title: str, severity: str, *, key: str = "", subject_kind: str = "repo",
            subject_id: Optional[str] = None, evidence: Iterable[Mapping[str, Any]] = (),
            source: str = "scan", parts: tuple = ()) -> None:
        out.append({"project_id": project_id, "source": source, "kind": kind, "title": title,
                    "fingerprint_parts": parts or (kind, repo, key),
                    "subject_kind": subject_kind, "subject_id": subject_id or repo,
                    "severity": severity, "evidence": [cite, *evidence],
                    "source_ref": f"dossier:{repo}:{kind}:{key}:{tag}",
                    "dedupe_window_seconds": 7 * 86400})

    if not facts.get("ci"):
        add("scan.no_ci", f"{repo} has no CI", "medium")
    tests = int((facts.get("tests") or {}).get("files") or 0)
    if not tests and int(facts.get("files") or 0) >= 5:
        add("scan.missing_tests", f"{repo} has no tests", "medium")
    for h in [h for h in facts.get("hotspots") or [] if not h.get("has_tests")][
            :MAX_SEEDED_HOTSPOTS]:
        add("scan.untested_hotspot", f"Hot file without tests: {h['file']}"[:300],
            "medium" if h["churn"] >= 200 else "low", key=h["file"], subject_kind="module",
            subject_id=f"{repo}:{h['file']}",
            evidence=[{"kind": "file", "ref": h["file"],
                       "note": f"{h['churn']} lines changed in {HOTSPOT_DAYS}d, "
                               f"{h['lines']} lines"}])
    top = ((facts.get("todos") or {}).get("clusters") or [None])[0]
    if top and int(top["count"]) >= TODO_CLUSTER_MIN:
        add("scan.todo_cluster", f"{top['count']} TODOs in {top['dir']}/"[:300], "low",
            key=top["dir"], evidence=[{"kind": "file", "ref": f} for f in top["files"][:3]])
    if not (facts.get("docs") or {}).get("readme"):
        add("scan.no_docs", f"{repo} has no README", "low")
    for adv in ((facts.get("advisories") or {}).get("advisories") or [])[:10]:
        primary = sorted(adv["ids"])[0]
        add("osv.vulnerable_dependency",
            f"{adv['name']} {adv['version']} has {primary}"[:300], "high", source="osv",
            key=f"{adv['name']}:{primary}", subject_kind="dependency", subject_id=adv["name"],
            parts=("osv", adv["name"], primary),
            evidence=[{"kind": "url", "ref": f"https://osv.dev/vulnerability/{primary}"}])
    return out


# ── building and storing ────────────────────────────────────────────────────────────────────────

def build(connection: Connection, *, project_id: str, repo: str, root: Optional[Path] = None,
          paths: Optional[list[str]] = None, head_sha: Optional[str] = None,
          actor: str = "boss", osv: Optional[Callable[[list], dict]] = None,
          seed_signals: bool = True) -> dict[str, Any]:
    """Read the repository (a checkout at `root`, else its file `paths`), store the project's
    ONE current dossier for it, and seed its signals. Returns the stored dossier."""
    from . import signals as signal_inbox

    repo = clean_text(repo, 200)
    if not repo:
        raise ValueError("a dossier is about one repository (owner/name)")
    if root is not None:
        facts = scan_tree(Path(root), osv=osv)
        read_from = "checkout"
        head = head_sha or _git(Path(root), "rev-parse", "HEAD").strip()
    else:
        facts = facts_from_paths(list(paths or []))
        read_from = "graph"
        head = head_sha or ""
    facts["boss"] = boss_view(connection, project_id, repo)
    existing = connection.execute(text(
        "SELECT id, version FROM project_dossiers WHERE project_id = :p AND repo = :r FOR UPDATE"),
        {"p": project_id, "r": repo}).mappings().first()
    dossier_id = existing["id"] if existing else new_id("dos")
    lights = highlights(facts)
    row = connection.execute(text(
        "INSERT INTO project_dossiers (id, project_id, repo, head_sha, facts, highlights, "
        "read_from, built_by) VALUES (:i, :p, :r, :h, CAST(:f AS JSONB), CAST(:l AS JSONB), "
        ":rf, :a) ON CONFLICT (project_id, repo) DO UPDATE SET head_sha = EXCLUDED.head_sha, "
        "facts = EXCLUDED.facts, highlights = EXCLUDED.highlights, read_from = "
        "EXCLUDED.read_from, built_by = EXCLUDED.built_by, built_at = now(), "
        "version = project_dossiers.version + 1 RETURNING *"),
        {"i": dossier_id, "p": project_id, "r": repo, "h": head or None,
         "f": json.dumps(facts, default=str), "l": json.dumps(lights), "rf": read_from,
         "a": actor}).mappings().one()
    seeded: list[dict[str, Any]] = []
    if seed_signals:
        for kwargs in seed(facts, project_id=project_id, repo=repo, dossier_id=dossier_id,
                           head=head or ""):
            got = signal_inbox.record_signal(connection, actor=actor, **kwargs)
            seeded.append({"signal_id": got["signal"]["id"], "kind": kwargs["kind"],
                           "outcome": got["outcome"]})
        row = connection.execute(text(
            "UPDATE project_dossiers SET seeded_signals = CAST(:s AS JSONB) WHERE id = :i "
            "RETURNING *"), {"s": json.dumps(seeded), "i": dossier_id}).mappings().one()
    emit(connection, project_id, "ambient.dossier_built", actor, "project", project_id,
         {"dossier_id": dossier_id, "repo": repo, "read_from": read_from,
          "highlights": lights, "seeded": len(seeded)})
    return serialize_row(row)


def get(connection: Connection, project_id: str, repo: Optional[str] = None
        ) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT * FROM project_dossiers WHERE project_id = :p AND (CAST(:r AS TEXT) IS NULL "
        "OR repo = :r) ORDER BY built_at DESC"), {"p": project_id, "r": repo}).mappings()
    return [serialize_row(r) for r in rows]


def after_index(engine: Any, settings: Any, *, project_id: str, repo: str, root: Path,
                head: str) -> Optional[dict[str, Any]]:
    """Called by the code-graph index once a repository's checkout is indexed: build the dossier
    from the same checkout. Its own transaction; a failure is logged, never fails the index."""
    if not bool(getattr(settings, "ambient_signals", 1)):
        return None
    use_osv = bool(int(getattr(settings, "ambient_dossier_osv", 1) or 0))
    try:
        with engine.begin() as c:
            return build(c, project_id=project_id, repo=repo, root=root, head_sha=head,
                         actor="boss", osv=osv_lookup if use_osv else None)
    except Exception:  # noqa: BLE001 — Discovery is a help, never a failed index
        log.warning("dossier build failed for %s in %s", repo, project_id, exc_info=True)
        return None


def checkout_for(connection: Connection, settings: Any, project_id: str,
                 repo: Optional[str]) -> tuple[Optional[str], Optional[Path], list[str]]:
    """(repo, the index's kept checkout if it is on disk, the graph's file list) for a request."""
    from ..knowledge import repo_index

    row = connection.execute(text(
        "SELECT b.id, b.full_name FROM github_repository_bindings b WHERE b.project_id = :p "
        "AND b.status = 'active' AND (CAST(:r AS TEXT) IS NULL OR lower(b.full_name) = "
        "lower(:r)) ORDER BY b.created_at LIMIT 1"), {"p": project_id, "r": repo}).mappings().first()
    if row is None:
        return None, None, []
    full = str(row["full_name"])
    path = repo_index._work_dir(repo_index.Limits.from_settings(settings), row["id"])  # noqa: SLF001
    files = connection.execute(text(
        "SELECT name FROM kg_nodes WHERE project_id = :p AND repo = :r AND kind = 'file' "
        "AND valid_to IS NULL LIMIT 20000"), {"p": project_id, "r": full}).scalars().all()
    return full, (path if (path / ".git").exists() else None), [str(f) for f in files]


__all__ = ["scan_tree", "facts_from_paths", "osv_lookup", "highlights", "seed", "build", "get",
           "after_index", "checkout_for"]
