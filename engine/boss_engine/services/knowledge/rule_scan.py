"""Scan for business rules (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §4.5, stream S3).

A person points the scan at an area or a package of the code graph. An agent reads that code,
class by class, and proposes the business rules it enforces, each with the lines that prove it.
Nothing it proposes is trusted: a rule is ``inferred`` until a person approves it in Rules review
(the ``rules_review`` gate, decision action ``review_proposed_rules``).

How it walks, from the code graph (never from names):

* The scope's classes (and each file's free functions, as one unit) are grouped by package. A
  unit's weight is structural: the tables it writes, the endpoints it exposes, the callees that
  write, how many other classes call it, how much it holds. Packages are visited heaviest first,
  and units heaviest first inside each.
* After a unit is read, the in-scope units it calls are read next, most-called first: a rule
  usually spans the class that decides and the class it hands the state to.
* Interfaces and test files are not read: they hold no rule of their own.

How it spends:

* Every model call goes through the engine contract (``run_role``, read-only) on whatever engine
  the project runs its review seat on, so it behaves the same on every engine; only quality may
  vary. The scripted conformance engine plays ``scripted_reply`` for it.
* Before every call it reserves the call through the budget governor, category ``knowledge``
  (``model_routing.GovernedChatAdapter``: refused means no call). It also checks the scan's own
  budget first, and stops before a call that could pass it.
* It stops at whichever limit comes first — files read, rules proposed, budget — and reports
  ``{files_read, rules_proposed, spend, stopped_by}`` with a sentence saying why.

How it writes:

* Every proposal goes through ``rules.propose_rule`` (the one writer of rules). Evidence is the
  symbol's graph key and the cited line range; the excerpt is BOSS's own copy of those lines,
  never the model's retyping of them.
* Provenance (agent role and name, workflow run, step, scan, model) is set in the LLM context
  before each proposal, where ``propose_rule`` reads it — never from the model's answer.
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import rules

log = logging.getLogger("boss.knowledge.rule_scan")

CAPABILITY = "scan_business_rules"
REVIEW_CAPABILITY = "rules_review"
#: The decision action of the review gate (decision_actions.WORKFLOW_GATE_ACTIONS).
REVIEW_ACTION = "review_proposed_rules"
#: The LLM-context stage every scan call runs under (telemetry, and the scripted engine's script).
STAGE = "rule_scan"
SKILL_ID = "knowledge.rule_scan"
BUDGET_CATEGORY = "knowledge"
DEFAULT_ROLE = "chief_architect"

DEFAULT_MAX_FILES = 60
DEFAULT_MAX_RULES = 25
DEFAULT_BUDGET_USD = 2.00
MAX_FILES_CEILING = 500
MAX_RULES_CEILING = 200
#: What one read-only session on a metered engine reserves before it is sent. A unit is one
#: class (at most MAX_LINES lines) and a short answer: well under this on any hosted model.
CALL_ESTIMATE_USD_MICROS = 150_000
#: The most of one unit that is read and shown to the agent.
MAX_LINES = 400
MAX_CHARS = 24_000
#: The most lines one evidence excerpt keeps.
EXCERPT_LINES = 12
EXCERPT_CHARS = 1_200
#: A read-only session needs very few turns: the code is in the prompt.
MAX_TURNS = 4

STOPPED_BY = ("scope_exhausted", "max_files", "max_rules", "budget", "budget_refused",
              "claim_lost", "engine_failed")

_STOP_WORDS = {
    "scope_exhausted": "every class in the scope was read",
    "max_files": "it read the most files it was allowed ({max_files})",
    "max_rules": "it proposed the most rules it was allowed ({max_rules})",
    "budget": "the next call could pass the scan's budget (${budget:.2f})",
    "budget_refused": "the budget governor refused the next call: {detail}",
    "claim_lost": "this step's claim was taken over, so it stopped rather than scan twice",
    "engine_failed": "the engine failed and the scan stopped: {detail}",
}

_MARK_OPEN = "=== RULES ==="
_MARK_CLOSE = "=== END RULES ==="
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|testFixtures|integrationTest)/|"
                        r"(_test|_spec|\.test|\.spec)\.[A-Za-z]+$|(^|/)test_[^/]+$|"
                        r"Tests?\.(java|kt|cs|scala)$")


class RuleScanRefused(ValueError):
    """The scan cannot start, for a reason a person has to change (scope, graph, rules)."""


# ── Scope ────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Scope:
    """What the scan reads: keys from the Graph's project tree (areas, packages, classes) or a
    file's key. Node ids (``kgn_…``) are resolved to their keys first (``resolve_ids``)."""

    repo: Optional[str]
    areas: tuple[str, ...] = ()
    #: (area, package) — a package key carries its area (``pkg:<repo>:<area>:<package>``).
    packages: tuple[tuple[str, str], ...] = ()
    #: class (or method, read as its class) ``symbol:`` keys
    symbols: tuple[str, ...] = ()
    #: file paths, from ``file:`` keys
    files: tuple[str, ...] = ()
    #: node ids still to resolve
    ids: tuple[str, ...] = ()
    #: ``module:`` keys (a package as the graph tools name it) still to resolve to packages
    modules: tuple[str, ...] = ()
    #: plain repository-relative paths (a file or a folder) still to resolve
    paths: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()

    def words(self) -> str:
        parts = [f"area {a}" for a in self.areas] + [f"package {p}" for _a, p in self.packages]
        parts += [f"class {s.rsplit(':', 1)[-1]}" for s in self.symbols]
        parts += [f"file {f}" for f in self.files]
        return ", ".join(parts) or "nothing"

    @property
    def keyed(self) -> dict[str, Any]:
        return {"areas": list(self.areas), "packages": [f"{a}:{p}" for a, p in self.packages],
                "symbols": list(self.symbols), "files": list(self.files)}


#: The most keys one scan's scope may name.
MAX_SCOPE_KEYS = 50
#: A plain repository path: segments of word characters, dots and dashes.
_PATH = re.compile(r"\.?/?[\w.\-]+(?:/[\w.\-]+)*/?")
_SCOPE_HELP = ("Name what to scan with any of: an area (area:<repo>:<area>), a package "
               "(pkg:<repo>:<area>:<package>, or module:<repo>:<package> as the graph tools "
               "show it), a class (its symbol: key), a file (file:<repo>:<path>), or a plain "
               "repository path to a file or a folder (fineract-loan/src/main/java/...).")


def _repo_and_rest(key: str, prefix: str, *, parts_after_repo: int) -> tuple[str, str]:
    """``<prefix>:<repo>:<rest>`` or ``<prefix>:<rest>`` (a repository with no name, as
    code_index._prefix writes it). Told apart by how many ``:``-separated parts the rest has:
    a file key's rest is its path (1), a symbol key's is path and name (2)."""
    body = key[len(prefix) + 1:]
    if body.count(":") >= parts_after_repo:
        head, _sep, rest = body.partition(":")
        return head, rest
    return "", body


def parse_scope(value: Any) -> Scope:
    """The scope a step was given: a list (or comma-separated text) of tree keys or node ids.
    One repository per scan; anything else is refused in words."""
    if isinstance(value, Mapping):
        value = value.get("keys") or value.get("scope") or []
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\n]", value)]
    if not isinstance(value, (list, tuple)):
        raise RuleScanRefused(f"scope must be a list of keys, not {type(value).__name__}. "
                              + _SCOPE_HELP)
    keys = [str(k).strip() for k in value if isinstance(k, (str, int)) and str(k).strip()]
    if len(keys) != len([k for k in value if k not in (None, "")]):
        raise RuleScanRefused("Every scope entry must be a key (text). " + _SCOPE_HELP)
    keys = list(dict.fromkeys(keys))
    if not keys:
        raise RuleScanRefused(
            "Choose what to scan: an area, a package or a class from the Graph's project tree.")
    if len(keys) > MAX_SCOPE_KEYS:
        raise RuleScanRefused(f"One scan can name at most {MAX_SCOPE_KEYS} keys; this names "
                              f"{len(keys)}. Choose an area or a package that holds them.")
    repos: set[str] = set()
    areas: list[str] = []
    packages: list[tuple[str, str]] = []
    symbols: list[str] = []
    files: list[str] = []
    ids: list[str] = []
    modules: list[str] = []
    paths: list[str] = []
    for key in keys:
        if key.startswith("area:"):
            parts = key.split(":", 2)
            if len(parts) != 3 or not parts[2]:
                raise RuleScanRefused(f"'{key}' is not an area key (area:<repo>:<area>).")
            repos.add(parts[1])
            areas.append(parts[2])
        elif key.startswith("pkg:"):
            parts = key.split(":", 3)
            if len(parts) != 4 or not parts[3]:
                raise RuleScanRefused(
                    f"'{key}' is not a package key (pkg:<repo>:<area>:<package>).")
            repos.add(parts[1])
            packages.append((parts[2], parts[3]))
        elif key.startswith("symbol:"):
            repo, rest = _repo_and_rest(key, "symbol", parts_after_repo=2)
            if ":" not in rest:
                raise RuleScanRefused(f"'{key}' is not a class key (symbol:<repo>:<path>:<name>).")
            repos.add(repo)
            symbols.append(key)
        elif key.startswith("file:"):
            repo, path = _repo_and_rest(key, "file", parts_after_repo=1)
            if not path:
                raise RuleScanRefused(f"'{key}' is not a file key (file:<repo>:<path>).")
            repos.add(repo)
            files.append(path)
        elif key.startswith("module:"):
            repo, module = _repo_and_rest(key, "module", parts_after_repo=1)
            if not module:
                raise RuleScanRefused(f"'{key}' is not a package key (module:<repo>:<package>).")
            repos.add(repo)
            modules.append(module)
        elif re.fullmatch(r"kgn_[A-Za-z0-9_]+", key):
            ids.append(key)
        elif ":" not in key and _PATH.fullmatch(key):
            # A repository path, as the repo browser and the Graph's file panel show it.
            paths.append(key.strip("/").removeprefix("./"))
        else:
            raise RuleScanRefused(f"'{key}' is not something the scan can read. " + _SCOPE_HELP)
    if len(repos) > 1:
        raise RuleScanRefused("One scan reads one repository; these keys name "
                              f"{len(repos)}: {', '.join(sorted(r or '(unnamed)' for r in repos))}.")
    return Scope(repo=next(iter(repos), "") or None, areas=tuple(dict.fromkeys(areas)),
                 packages=tuple(dict.fromkeys(packages)), symbols=tuple(dict.fromkeys(symbols)),
                 files=tuple(dict.fromkeys(files)), ids=tuple(ids),
                 modules=tuple(dict.fromkeys(modules)), paths=tuple(dict.fromkeys(paths)),
                 keys=tuple(keys))


def resolve_ids(connection: Connection, project_id: str, scope: Scope) -> Scope:
    """Resolve everything in `scope` that is not yet a tree key — node ids, ``module:`` keys and
    plain repository paths — to the keys the scan reads, or refuse in words. The graph tools an
    agent uses (kg_schema / kg_cypher) name packages ``module:<repo>:<package>`` while the tree
    names them ``pkg:<repo>:<area>:<package>``, and a person names code by its path; two credit
    scans deadlettered on exactly that (wfr_5af8917f…, wfr_ab044938…, brec_40c192798f564212a02d)."""
    if not (scope.ids or scope.modules or scope.paths):
        return scope
    pending = set(scope.ids) | {k for k in scope.keys if k.startswith("module:")} \
        | {k for k in scope.keys if ":" not in k and not re.fullmatch(r"kgn_[A-Za-z0-9_]+", k)}
    keys = [k for k in scope.keys if k not in pending]
    if scope.ids:
        rows = connection.execute(
            text("SELECT id, kind, node_key FROM kg_nodes WHERE project_id = :p "
                 "AND id = ANY(:ids) AND valid_to IS NULL"),
            {"p": project_id, "ids": list(scope.ids)},
        ).mappings().all()
        found = {r["id"]: r for r in rows}
        for node_id in scope.ids:
            row = found.get(node_id)
            if row is None:
                raise RuleScanRefused(f"'{node_id}' is not a node of this project's graph.")
            if row["kind"] not in ("symbol", "file"):
                raise RuleScanRefused(
                    f"'{node_id}' is a {row['kind']}, not code. Scan an area, a package, a class "
                    "or a file.")
            keys.append(str(row["node_key"]))
    for module in scope.modules:
        rows = connection.execute(text(
            "SELECT DISTINCT repo, area, package FROM kg_view_tree WHERE project_id = :p "
            "AND level = 'package' AND package = :m AND (:r = '' OR repo = :r)"),
            {"p": project_id, "m": module, "r": scope.repo or ""}).mappings().all()
        if not rows:
            raise RuleScanRefused(
                f"module:{module} names no package in this project's graph. Check the repository "
                "is indexed (Graph › its status) and the package is in Graph › Tree.")
        keys += [f"pkg:{r['repo']}:{r['area']}:{r['package']}" for r in rows]
    for path in scope.paths:
        keys += _keys_for_path(connection, project_id, scope.repo or "", path)
    keys = list(dict.fromkeys(keys))
    if len(keys) > MAX_SCOPE_KEYS:
        raise RuleScanRefused(
            f"That scope covers {len(keys)} packages or files; one scan reads at most "
            f"{MAX_SCOPE_KEYS}. Choose a narrower folder, a package or an area.")
    return parse_scope(keys)


def _keys_for_path(connection: Connection, project_id: str, repo: str, path: str) -> list[str]:
    """A repository path → its file key (a file), else the packages of the classes under it (a
    folder), else every file under it. Refused when nothing indexed is there."""
    exact = connection.execute(text(
        "SELECT node_key FROM kg_nodes WHERE project_id = :p AND kind = 'file' "
        "AND valid_to IS NULL AND path = :path AND (:r = '' OR repo = :r) LIMIT 2"),
        {"p": project_id, "path": path, "r": repo}).scalars().all()
    if exact:
        return [str(k) for k in exact]
    prefix = path.rstrip("/") + "/%"
    packages = connection.execute(text(
        "SELECT DISTINCT t.repo, t.area, t.package FROM kg_view_tree t "
        "JOIN kg_nodes n ON n.id = t.node_id AND n.valid_to IS NULL "
        "WHERE t.project_id = :p AND t.level = 'class' AND t.package IS NOT NULL "
        "AND n.path LIKE :prefix AND (:r = '' OR t.repo = :r)"),
        {"p": project_id, "prefix": prefix, "r": repo}).mappings().all()
    if packages:
        return [f"pkg:{r['repo']}:{r['area']}:{r['package']}" for r in packages]
    files = connection.execute(text(
        "SELECT node_key FROM kg_nodes WHERE project_id = :p AND kind = 'file' "
        "AND valid_to IS NULL AND path LIKE :prefix AND (:r = '' OR repo = :r) "
        "ORDER BY path LIMIT :n"),
        {"p": project_id, "prefix": prefix, "r": repo, "n": MAX_SCOPE_KEYS + 1}).scalars().all()
    if files:
        return [str(k) for k in files]
    raise RuleScanRefused(
        f"'{path}' is not a file or folder of this project's indexed code. Check the path from "
        "the repository root, or pick it in Graph › Tree.")


def validate_scope(connection: Connection, project_id: str, scope: Any, *,
                   exclude_tests: Any = None) -> list[str]:
    """THE SCAN'S SCOPE, CHECKED AT START (before anything is queued or spent): parsed, resolved
    and holding code to read — the same three checks the scan step makes. Returns the resolved
    keys the run should carry; raises RuleScanRefused in words."""
    parsed = resolve_ids(connection, project_id, parse_scope(scope))
    units = units_in_scope(connection, project_id, parsed,
                           exclude_tests=_excludes_tests(exclude_tests))
    if not units:
        raise RuleScanRefused(
            f"There is no code in {parsed.words()} to read. Check the repository is indexed "
            "(Graph › its status) and the key or path comes from this project's tree.")
    return list(parsed.keys)


def is_test_path(path: str) -> bool:
    """Test code (it exercises rules; it does not enforce them): left out unless asked."""
    return bool(_TEST_PATH.search(str(path or "")))


def _excludes_tests(value: Any) -> bool:
    """`exclude_tests`: on by default; "false", "no", "0" or False read the tests too."""
    if value is None or value == "":
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "no", "0", "off")


# ── Limits ───────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Limits:
    max_files: int = DEFAULT_MAX_FILES
    max_rules: int = DEFAULT_MAX_RULES
    budget_usd_micros: int = int(DEFAULT_BUDGET_USD * 1_000_000)
    #: Said in the report when a limit was changed from what was asked (the project's cap).
    notes: tuple[str, ...] = ()


def _int(value: Any, default: int, name: str, ceiling: int) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        raise RuleScanRefused(f"{name} must be a whole number, not {value!r}.") from None
    if number < 1:
        raise RuleScanRefused(f"{name} must be at least 1.")
    if number > ceiling:
        raise RuleScanRefused(f"{name} can be at most {ceiling} in one scan.")
    return number


def parse_limits(inputs: Mapping[str, Any], *, cap_usd_micros: int = 0) -> Limits:
    """The scan's limits from its inputs. The budget is at most the project's cap: a larger
    budget is lowered to the cap and the report says so."""
    max_files = _int(inputs.get("max_files"), DEFAULT_MAX_FILES, "max_files", MAX_FILES_CEILING)
    max_rules = _int(inputs.get("max_rules"), DEFAULT_MAX_RULES, "max_rules", MAX_RULES_CEILING)
    raw = inputs.get("budget_usd")
    try:
        budget = DEFAULT_BUDGET_USD if raw in (None, "") else float(raw)
    except (TypeError, ValueError):
        raise RuleScanRefused(f"budget_usd must be an amount in dollars, not {raw!r}.") from None
    if not budget > 0:
        raise RuleScanRefused("budget_usd must be more than $0.00.")
    micros = int(round(budget * 1_000_000))
    notes: list[str] = []
    if cap_usd_micros and micros > cap_usd_micros:
        notes.append(f"The budget asked (${budget:.2f}) is over the project's cap; the scan "
                     f"uses the cap, ${cap_usd_micros / 1_000_000:.2f}.")
        micros = int(cap_usd_micros)
    return Limits(max_files=max_files, max_rules=max_rules, budget_usd_micros=micros,
                  notes=tuple(notes))


# ── The code graph ───────────────────────────────────────────────────────────────────────────

@dataclass
class Unit:
    """One thing the agent reads in one call: a class, or a file's free functions."""

    key: str
    name: str
    path: str
    repo: str
    area: str
    package: str
    line: int
    end_line: int
    is_class: bool = True
    type_kind: str = ""
    #: (key, name, line, end_line) of the class itself and each of its members.
    symbols: list[tuple[str, str, int, int]] = field(default_factory=list)
    writes: set[str] = field(default_factory=set)
    endpoints: set[str] = field(default_factory=set)
    callers: set[str] = field(default_factory=set)
    #: unit key -> calls into it from this unit
    callees: dict[str, int] = field(default_factory=dict)
    score: float = 0.0

    def facts(self, names: Mapping[str, str]) -> list[str]:
        out = []
        if self.writes:
            out.append("writes tables: " + ", ".join(sorted(self.writes)[:12]))
        if self.endpoints:
            out.append("serves endpoints: " + ", ".join(sorted(self.endpoints)[:12]))
        if self.callers:
            out.append(f"called from {len(self.callers)} other classes")
        if self.callees:
            top = sorted(self.callees.items(), key=lambda kv: -kv[1])[:8]
            out.append("calls: " + ", ".join(f"{names.get(k, k.rsplit(':', 1)[-1])} ({n})"
                                             for k, n in top))
        return out


def _area_of(path: str) -> str:
    return (path or "").split("/", 1)[0]


def _owner_key(key: str, classes: set[str]) -> Optional[str]:
    """The class a member key belongs to: the longest class key it extends with ``.member``."""
    head = key
    while "." in head.rsplit(":", 1)[-1]:
        head = head.rsplit(".", 1)[0]
        if head in classes:
            return head
    return None


def _scope_sql(scope: Scope, *, nested_areas: bool) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for i, area in enumerate(scope.areas):
        params[f"a{i}"] = f"{area}/%"
        if nested_areas:
            params[f"an{i}"] = f"%/{area}/%"
            clauses.append(f"(n.path LIKE :a{i} OR n.path LIKE :an{i})")
        else:
            clauses.append(f"n.path LIKE :a{i}")
    for i, (area, package) in enumerate(scope.packages):
        params[f"pa{i}"], params[f"pk{i}"] = f"{area}/%", package
        clauses.append(f"(n.attrs->>'module' = :pk{i} AND n.path LIKE :pa{i})")
    for i, key in enumerate(scope.symbols):
        params[f"s{i}"], params[f"sl{i}"] = key, key.replace("%", r"\%") + ".%"
        clauses.append(f"(n.node_key = :s{i} OR n.node_key LIKE :sl{i})")
    if scope.files:
        params["files"] = list(scope.files)
        clauses.append("n.path = ANY(:files)")
    return " OR ".join(clauses) or "FALSE", params


_SYMBOL_COLUMNS = (
    "n.id, n.node_key, n.name, n.path, COALESCE(n.repo, '') AS repo, "
    "n.attrs->>'symbol_kind' AS symbol_kind, n.attrs->>'type_kind' AS type_kind, "
    "n.attrs->>'module' AS module, n.attrs->>'line' AS line, n.attrs->>'end_line' AS end_line")


def _has_rollup(connection: Connection, project_id: str) -> bool:
    """Whether the Graph's tree (view_rollup) is built for this project: its keys are the ones
    the console hands the scan, so it is the first place they are resolved."""
    try:
        with connection.begin_nested():
            return connection.execute(
                text("SELECT 1 FROM kg_view_tree WHERE project_id = :p LIMIT 1"),
                {"p": project_id}).first() is not None
    except Exception:  # noqa: BLE001 — no rollup table on this install: read the paths
        return False


def _symbols_in_tree(connection: Connection, project_id: str, scope: Scope
                     ) -> list[dict[str, Any]]:
    """The scope's symbols as the Graph's tree files them: its area and package keys exactly
    (view_rollup's area rule, its package fallback), a class with everything under it."""
    symbols = list(scope.symbols)
    prefixes = [k.replace("%", r"\%") + ".%" for k in symbols]
    rows = connection.execute(
        text(
            "SELECT " + _SYMBOL_COLUMNS + ", t.area AS tree_area, t.package AS tree_package "
            "FROM kg_view_tree t JOIN kg_nodes n ON n.id = t.node_id AND n.valid_to IS NULL "
            "WHERE t.project_id = :p AND t.level IN ('class', 'method') AND t.repo = :repo "
            "AND (t.area = ANY(:areas) "
            "     OR ('pkg:' || t.repo || ':' || t.area || ':' || COALESCE(t.package, '')) "
            "        = ANY(:pkgs) "
            "     OR t.key = ANY(:syms) OR t.key LIKE ANY(:prefixes) "
            "     OR n.path = ANY(:files))"
        ),
        {"p": project_id, "repo": scope.repo or "", "areas": list(scope.areas),
         "pkgs": [f"pkg:{scope.repo or ''}:{a}:{p}" for a, p in scope.packages],
         "syms": symbols, "prefixes": prefixes, "files": list(scope.files)},
    ).mappings().all()
    return [dict(r) for r in rows]


def _symbols_in_scope(connection: Connection, project_id: str, scope: Scope,
                      *, nested_areas: bool = False) -> list[dict[str, Any]]:
    where, params = _scope_sql(scope, nested_areas=nested_areas)
    rows = connection.execute(
        text(
            "SELECT " + _SYMBOL_COLUMNS + " "
            "FROM kg_nodes n WHERE n.project_id = :p AND n.kind = 'symbol' "
            "AND n.valid_to IS NULL AND COALESCE(n.repo, '') = :repo AND (" + where + ")"
        ),
        {"p": project_id, "repo": scope.repo or "", **params},
    ).mappings().all()
    return [dict(r) for r in rows]


def _num(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _with_method_owners(connection: Connection, project_id: str, scope: Scope) -> Scope:
    """A method's key reads as its class: the rule a method enforces is read in its class."""
    if not scope.symbols:
        return scope
    methods = {r[0] for r in connection.execute(
        text("SELECT node_key FROM kg_nodes WHERE project_id = :p AND kind = 'symbol' "
             "AND valid_to IS NULL AND node_key = ANY(:k) "
             "AND attrs->>'symbol_kind' = 'method'"),
        {"p": project_id, "k": list(scope.symbols)}).all()}
    if not methods:
        return scope
    owners = [k.rsplit(".", 1)[0] if k in methods else k for k in scope.symbols]
    return Scope(repo=scope.repo, areas=scope.areas, packages=scope.packages,
                 symbols=tuple(dict.fromkeys(owners)), files=scope.files, ids=scope.ids,
                 keys=scope.keys)


def units_in_scope(connection: Connection, project_id: str, scope: Scope, *,
                   exclude_tests: bool = True) -> list[Unit]:
    """Every unit the scope holds, weighed from its graph edges (see the module docstring)."""
    scope = _with_method_owners(connection, project_id, scope)
    rows = (_symbols_in_tree(connection, project_id, scope)
            if _has_rollup(connection, project_id) else [])
    if not rows:
        rows = _symbols_in_scope(connection, project_id, scope)
    if not rows and scope.areas:
        # An area below the repository's top level (view_rollup's 80% rule).
        rows = _symbols_in_scope(connection, project_id, scope, nested_areas=True)
    if not rows:
        return []
    classes = {r["node_key"] for r in rows if r["symbol_kind"] == "class"}
    units: dict[str, Unit] = {}
    unit_of: dict[str, str] = {}      # node id -> unit key
    by_key: dict[str, dict[str, Any]] = {r["node_key"]: r for r in rows}

    def unit_for(row: Mapping[str, Any]) -> Unit:
        owner = row["node_key"] if row["node_key"] in classes else _owner_key(
            row["node_key"], classes)
        if owner:
            head = by_key[owner]
            key, name, line, end = owner, head["name"], _num(head["line"], 1), \
                _num(head["end_line"], _num(head["line"], 1))
            is_class, type_kind = True, str(head["type_kind"] or "")
        else:
            key, name = f"file:{row['repo']}:{row['path']}", Path(row["path"] or "").name
            line, end, is_class, type_kind = 0, 0, False, ""
        unit = units.get(key)
        if unit is None:
            path = str(row["path"] or "")
            unit = units[key] = Unit(
                key=key, name=name, path=path, repo=str(row["repo"] or ""),
                area=str(row.get("tree_area") or _area_of(path)),
                package=str(row.get("tree_package") or row["module"]
                            or Path(path).parent.as_posix()),
                line=line, end_line=end, is_class=is_class, type_kind=type_kind)
        return unit

    for row in rows:
        unit = unit_for(row)
        line = _num(row["line"], 1)
        unit.symbols.append((row["node_key"], row["name"], line, _num(row["end_line"], line)))
        unit_of[row["id"]] = unit.key
        if not unit.is_class:
            unit.line = min(unit.line or line, line)
            unit.end_line = max(unit.end_line, _num(row["end_line"], line))
    ids = list(unit_of)
    out_edges = connection.execute(
        text(
            "SELECT e.src_id, e.dst_id, e.kind, COALESCE((e.attrs->>'count')::int, 1) AS n, "
            "d.kind AS dst_kind, d.name AS dst_name "
            "FROM kg_edges e JOIN kg_nodes d ON d.id = e.dst_id "
            "WHERE e.project_id = :p AND e.src_id = ANY(:ids) AND e.valid_to IS NULL "
            "AND e.kind IN ('calls', 'dispatches_to', 'writes', 'exposes')"
        ),
        {"p": project_id, "ids": ids},
    ).mappings().all()
    for edge in out_edges:
        unit = units[unit_of[edge["src_id"]]]
        if edge["kind"] == "writes" and edge["dst_kind"] == "table":
            unit.writes.add(str(edge["dst_name"]))
        elif edge["kind"] == "exposes":
            unit.endpoints.add(str(edge["dst_name"]))
        elif edge["kind"] in ("calls", "dispatches_to"):
            target = unit_of.get(edge["dst_id"])
            if target and target != unit.key:
                unit.callees[target] = unit.callees.get(target, 0) + int(edge["n"] or 1)
    in_edges = connection.execute(
        text(
            "SELECT e.dst_id, s.node_key AS src_key "
            "FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id "
            "WHERE e.project_id = :p AND e.dst_id = ANY(:ids) AND e.valid_to IS NULL "
            "AND e.kind IN ('calls', 'dispatches_to')"
        ),
        {"p": project_id, "ids": ids},
    ).mappings().all()
    for edge in in_edges:
        unit = units[unit_of[edge["dst_id"]]]
        src = str(edge["src_key"] or "")
        caller = unit_of.get(src) or _owner_key(src, classes) or src.rsplit(".", 1)[0]
        if caller and caller != unit.key:
            unit.callers.add(caller)
    readable = [u for u in units.values() if _readable(u, exclude_tests=exclude_tests)]
    for unit in readable:
        unit.score = weigh(unit, units)
    return readable


def _readable(unit: Unit, *, exclude_tests: bool = True) -> bool:
    if exclude_tests and is_test_path(unit.path or ""):
        return False
    if unit.type_kind in ("interface", "annotation"):
        return False
    return bool(unit.path)


def weigh(unit: Unit, units: Mapping[str, Unit]) -> float:
    """Where rules are likely, from structure alone: state written, entry points served,
    state written by what it calls, how many depend on it, how much it holds."""
    writing_callees = sum(1 for k in unit.callees if units.get(k) and units[k].writes)
    return (6.0 * len(unit.writes) + 3.0 * len(unit.endpoints) + 2.0 * writing_callees
            + min(len(unit.callers), 20) + 0.5 * min(len(unit.symbols), 20))


def walk_order(units: Sequence[Unit]) -> Iterable[Unit]:
    """Module by module, heaviest first; after each unit, the units it calls (most-called
    first) before the walk goes on. Each unit once."""
    by_key = {u.key: u for u in units}
    packages: dict[tuple[str, str], list[Unit]] = {}
    for unit in units:
        packages.setdefault((unit.area, unit.package), []).append(unit)
    for members in packages.values():
        members.sort(key=lambda u: (-u.score, u.path, u.line))

    def package_weight(item: tuple[tuple[str, str], list[Unit]]) -> tuple:
        (area, package), members = item
        return (-sum(u.score for u in members[:5]), area, package)

    seen: set[str] = set()
    for _package, members in sorted(packages.items(), key=package_weight):
        for head in members:
            stack = [head]
            while stack:
                unit = stack.pop()
                if unit.key in seen:
                    continue
                seen.add(unit.key)
                yield unit
                follow = sorted((k for k in unit.callees if k in by_key and k not in seen),
                                key=lambda k: (unit.callees[k], by_key[k].score))
                stack.extend(by_key[k] for k in follow)   # most-called popped first


# ── Reading the code ─────────────────────────────────────────────────────────────────────────

#: reader(path, start, end, repo) -> {"content", "start", "end"} or {"error"}
Reader = Callable[[str, int, int, str], Mapping[str, Any]]


def repo_reader(connection: Connection, settings: Any, project_id: str, actor: str) -> Reader:
    """Read through ``repo.read`` (the read_code tool, project-pinned), as every agent does."""
    from ..skill_executors import SkillExecutorContext, execute_repo_read

    context = SkillExecutorContext(settings=settings, connection=connection,
                                   project_id=project_id, actor=actor)

    def read(path: str, start: int, end: int, repo: str) -> Mapping[str, Any]:
        args: dict[str, Any] = {"tool": "read_code", "file": path, "start": max(1, start),
                                "end": max(start, end)}
        if repo:
            args["repo"] = repo
        try:
            out = execute_repo_read(context, args).get("result") or {}
        except Exception as error:  # noqa: BLE001 — one unreadable file is said, not fatal
            return {"error": f"{type(error).__name__}: {error}"[:300]}
        if out.get("ok") is False or "content" not in out:
            return {"error": str(out.get("error") or "the file could not be read")[:300]}
        return out

    return read


def _numbered(content: str, start: int) -> tuple[str, dict[int, str]]:
    lines = content.splitlines()
    held = {start + i: line for i, line in enumerate(lines)}
    return "\n".join(f"{n:>5} | {line}" for n, line in held.items()), held


# ── The agent's question and answer ─────────────────────────────────────────────────────────

SYSTEM = (
    "You are reading one part of a codebase to find the BUSINESS RULES it enforces: what a "
    "domain expert would recognise as a rule of the business, not of the framework. Rules are "
    "validations (what is refused, and when), calculations (how an amount, a rate, a date is "
    "worked out), limits (a maximum, a minimum, a count), state transitions (which state may "
    "follow which, and who may move it) and authorization (who may do it). Mechanics — null "
    "checks, logging, mapping, wiring — are not rules. Propose only what the code in front of "
    "you enforces, and cite the exact lines that enforce it. Never invent a rule the code does "
    "not show; answer with an empty list when there is none.")


def scan_prompt(unit: Unit, code: str, *, start: int, end: int, facts: Sequence[str],
                remaining: int, scope_words: str, focus: str = "") -> str:
    symbols = [s for s in unit.symbols if s[2] <= end and s[3] >= start] or unit.symbols
    listing = "\n".join(f"- {name} (lines {line}-{last})"
                        for _key, name, line, last in sorted(symbols, key=lambda s: s[2]))
    graph = "\n".join(f"- {f}" for f in facts) or "- nothing beyond its own code"
    return "\n\n".join(part for part in [
        SYSTEM,
        f"SCOPE: {scope_words}. THIS PART: {unit.name} in {unit.path}, lines {start}-{end}.",
        (f"WHAT THE PERSON WHO STARTED THE SCAN CARES ABOUT MOST: {focus}" if focus else ""),
        "WHAT THE CODE GRAPH SAYS ABOUT IT:\n" + graph,
        "SYMBOLS (name each piece of evidence by one of these names):\n" + listing,
        f"THE CODE ({unit.path}, each line prefixed by its number):\n{code}",
        f"Answer with at most {remaining} rules, as a JSON list between the two marker lines, "
        "and nothing else between them:\n"
        f"{_MARK_OPEN}\n"
        '[{"title": "a short name", "statement": "the rule, in one sentence a domain expert '
        'would sign", "category": "validation|calculation|limit|state_transition|'
        'authorization|other", "rationale": "how these lines enforce it", "confidence": 0.0, '
        '"evidence": [{"symbol": "<a name from SYMBOLS>", "lines": [<first>, <last>]}]}]\n'
        f"{_MARK_CLOSE}\n"
        "Confidence is how sure you are that the rule is real and stated correctly (0 to 1). "
        "Cite the narrowest lines that enforce the rule. Answer [] when this code enforces none.",
    ] if part)


def parse_answer(answer: str) -> Optional[list[dict[str, Any]]]:
    """The proposals in an answer, or None when it has no readable list."""
    body = str(answer or "")
    if _MARK_OPEN in body:
        body = body.split(_MARK_OPEN, 1)[1]
        body = body.split(_MARK_CLOSE, 1)[0]
    body = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", body.strip())
    start, end = body.find("["), body.rfind("]")
    if start < 0 or end < start:
        return None
    try:
        parsed = json.loads(body[start:end + 1])
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    return [p for p in parsed if isinstance(p, dict)]


def _lines_of(value: Any) -> Optional[tuple[int, int]]:
    if isinstance(value, (list, tuple)) and value:
        first, last = _num(value[0], -1), _num(value[-1], -1)
    elif isinstance(value, str) and re.fullmatch(r"\s*\d+\s*(-\s*\d+\s*)?", value):
        bits = [int(b) for b in re.findall(r"\d+", value)]
        first, last = bits[0], bits[-1]
    elif isinstance(value, int) and not isinstance(value, bool):
        first = last = value
    else:
        return None
    if first < 1 or last < first:
        return None
    return first, last


def _resolve_symbol(unit: Unit, named: Any) -> Optional[tuple[str, str]]:
    """(key, name) of the unit's symbol an answer names: the full key, the qualified name, or
    its last segment when that is unambiguous."""
    named = str(named or "").strip()
    if not named:
        return None
    for key, name, _l, _e in unit.symbols:
        if named in (key, name):
            return key, name
    tail = [(k, n) for k, n, _l, _e in unit.symbols if n.rsplit(".", 1)[-1] == named.rsplit(".", 1)[-1]]
    return tail[0] if len(tail) == 1 else None


def validate_proposal(unit: Unit, proposal: Mapping[str, Any], held: Mapping[int, str],
                      *, package_known: bool = True
                      ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """The propose_rule arguments for one proposal, or why it was set aside. Evidence must name
    one of the unit's symbols and lines that were read; the excerpt is those lines, as read."""
    title = " ".join(str(proposal.get("title") or "").split())[:160]
    statement = " ".join(str(proposal.get("statement") or "").split())[:1000]
    if not statement:
        return None, "it states no rule"
    if not title:
        title = statement[:120]
    category = str(proposal.get("category") or "other").strip().lower()
    if category not in rules.CATEGORIES:
        category = "other"
    try:
        confidence = min(1.0, max(0.0, float(proposal.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.5
    evidence: list[dict[str, Any]] = []
    for item in proposal.get("evidence") or []:
        if not isinstance(item, Mapping):
            continue
        resolved = _resolve_symbol(unit, item.get("symbol"))
        lines = _lines_of(item.get("lines"))
        if not resolved or not lines:
            continue
        first, last = lines
        if first not in held or last not in held:
            continue
        cited = [held[n] for n in range(first, min(last, first + EXCERPT_LINES - 1) + 1)
                 if n in held]
        evidence.append({"symbol": resolved[0], "lines": [first, last],
                         "excerpt": "\n".join(cited)[:EXCERPT_CHARS]})
    if not evidence:
        return None, "its evidence names no symbol and lines of the code that was read"
    return {
        "statement": statement, "title": title, "category": category,
        "scope": {"repo": unit.repo or None, "areas": [unit.area] if unit.area else [],
                  # The tree's key, which says which repository; a repository with no name
                  # has one package namespace, so its package is named plainly.
                  "packages": ([f"pkg:{unit.repo}:{unit.area}:{unit.package}" if unit.repo
                                else unit.package] if unit.package and package_known else []),
                  "symbols": list(dict.fromkeys(e["symbol"] for e in evidence))},
        "evidence": evidence,
        "rationale": " ".join(str(proposal.get("rationale") or "").split())[:1000],
        "confidence": confidence,
    }, None


def scripted_reply(prompt: str) -> str:
    """The scripted conformance engine's answer to a scan prompt (scripted_engine, stage
    ``rule_scan``): one rule on the first symbol the prompt lists, citing its first line, so
    BOSS's whole scan path — budget, limits, evidence, propose_rule, the report — runs for free."""
    listed = re.findall(r"^- (\S+) \(lines (\d+)-(\d+)\)$", prompt or "", re.M)
    if not listed:
        return f"Nothing to propose.\n{_MARK_OPEN}\n[]\n{_MARK_CLOSE}"
    name, first, _last = listed[0]
    rule = {"title": f"{name} checks its input", "statement": f"{name} refuses input it does "
            "not accept.", "category": "validation", "rationale": "scripted conformance answer",
            "confidence": 0.5, "evidence": [{"symbol": name, "lines": [int(first), int(first)]}]}
    return f"Read {name}.\n{_MARK_OPEN}\n{json.dumps([rule])}\n{_MARK_CLOSE}"


# ── Spending ─────────────────────────────────────────────────────────────────────────────────

class BudgetRefused(RuntimeError):
    """The budget governor refused the reservation: nothing was called."""


class Governor:
    """Reserve one call before it is sent, settle it with what it cost, release it if it
    failed — through ``model_routing.GovernedChatAdapter``, the one reserve/receipt/settle path
    every model call takes, in budget category ``knowledge``."""

    def __init__(self, connection: Connection, settings: Any, project_id: str, *, engine: str,
                 ref: str):
        from ..model_routing import GovernedChatAdapter, ModelChoice, turn_estimate_usd_micros

        self.choice = ModelChoice(engine, "the project's review engine (rule scan)")
        meters = turn_estimate_usd_micros(self.choice, settings) > 0
        #: What one call reserves: nothing on a subscription or local engine (it costs no
        #: money, and is still refused once the cap is spent), a session's worth otherwise.
        self.estimate_usd_micros = CALL_ESTIMATE_USD_MICROS if meters else 0
        self._governed = GovernedChatAdapter(
            None, connection, settings, project_id, skill_id=SKILL_ID, ref=ref,
            estimate_usd_micros=self.estimate_usd_micros, choice=self.choice,
            category=BUDGET_CATEGORY)

    #: A deferred reservation (a busy admission, a full minute) is waited out this many times,
    #: for its own retry_after (at most DEFER_WAIT_CAP_S each), before the scan stops on it.
    DEFER_TRIES = 3
    DEFER_WAIT_CAP_S = 5.0

    def reserve(self) -> Any:
        import time

        from ..model_routing import ModelSpendDeferred, ModelSpendRefused

        for attempt in range(self.DEFER_TRIES + 1):
            try:
                return self._governed._open()
            except ModelSpendRefused as refused:
                raise BudgetRefused(str(refused)) from refused
            except ModelSpendDeferred as deferred:
                if attempt == self.DEFER_TRIES:
                    raise BudgetRefused(str(deferred)) from deferred
                wait = float(getattr(deferred, "retry_after_seconds", None) or 1.0)
                time.sleep(min(max(wait, 0.2), self.DEFER_WAIT_CAP_S))
        raise BudgetRefused("the budget did not admit the call")  # pragma: no cover

    def settle(self, held: Any, usage: Mapping[str, Any]) -> int:
        """Settle; return the money it cost (0 when included in a subscription)."""
        from ..model_routing import turn_is_included
        from ..openai_adapter import estimate_cost_usd_micros

        self._governed._settle(held, dict(usage))
        if turn_is_included(self.choice, dict(usage)):
            return 0
        return int(estimate_cost_usd_micros(dict(usage)) or 0)

    def fail(self, held: Any, error: BaseException) -> None:
        self._governed._fail(held, error)


def _usage_of(result: Mapping[str, Any]) -> dict[str, Any]:
    usage = dict(result.get("usage") or {}) if isinstance(result.get("usage"), Mapping) else {}
    if not result.get("cost_unreported") and result.get("cost_usd_micros") is not None:
        usage["cost_usd_micros"] = int(result.get("cost_usd_micros") or 0)
    if result.get("model") and not usage.get("model"):
        usage["model"] = result["model"]
    return usage


# ── The scan ────────────────────────────────────────────────────────────────────────────────

def _review_engine(settings: Any) -> str:
    return str(getattr(settings, "code_review_engine", None)
               or getattr(settings, "code_agent_engine", None) or "claude_code").lower()


def _scan_agent(connection: Connection, settings: Any, project_id: str) -> tuple[Any, str]:
    """The project's review-seat engine under the engine contract, and its name."""
    from ..code_agent import make_code_agent

    engine = _review_engine(settings)
    return make_code_agent(settings, engine=engine, connection=connection,
                           project_id=project_id), engine


def _check_entitlement(settings: Any, project_id: str) -> None:
    """The add-on boundary (§3.7), the rules layer's own answer: Community allows it; a
    commercial port may not. Asked before anything is read or spent."""
    verdict = rules.entitlement(project_id)
    if not verdict.get("allowed"):
        raise RuleScanRefused(str(verdict.get("reason") or
                                  "Business rules are not included in this edition."))


def _project_cap(connection: Connection, settings: Any, project_id: str) -> int:
    try:
        from ..budget import BudgetGovernor

        return int(BudgetGovernor(settings).status_for_project(connection, project_id)
                   .get("cap_usd_micros") or 0)
    except Exception:  # noqa: BLE001 — no cap to read is no cap to lower the budget to
        return 0


def scan(
    connection: Connection,
    settings: Any,
    *,
    project_id: str,
    scope: Any,
    inputs: Optional[Mapping[str, Any]] = None,
    scan_id: Optional[str] = None,
    agent_role: str = DEFAULT_ROLE,
    agent_name: str = "",
    workflow_run_id: str = "",
    step_id: str = "scan",
    actor: str = "workflow_runner",
    agent: Any = None,
    engine: Optional[str] = None,
    reader: Optional[Reader] = None,
    governor: Any = None,
    keep_claim: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Scan `scope` for business rules and propose them. Returns the report (see the module
    docstring). Raises RuleScanRefused before any call when it cannot start."""
    from ..llm_telemetry import get_llm_context, set_llm_context

    _check_entitlement(settings, project_id)
    parsed = resolve_ids(connection, project_id, parse_scope(scope))
    limits = parse_limits(inputs or {}, cap_usd_micros=_project_cap(connection, settings,
                                                                    project_id))
    scan_id = scan_id or f"rscan_{uuid.uuid4().hex[:16]}"
    exclude_tests = _excludes_tests((inputs or {}).get("exclude_tests"))
    units = units_in_scope(connection, project_id, parsed, exclude_tests=exclude_tests)
    if not units:
        raise RuleScanRefused(
            f"There is no code in {parsed.words()} to read"
            + (" outside its tests (set exclude_tests false to read them)" if exclude_tests
               else "")
            + ". Check the repository is indexed (Graph › its status) and the key or path "
              "comes from this project's tree.")
    if agent is None:
        agent, engine = _scan_agent(connection, settings, project_id)
    engine = engine or str(getattr(agent, "engine", "") or "")
    governor = governor or Governor(connection, settings, project_id, engine=engine, ref=scan_id)
    estimate = int(getattr(governor, "estimate_usd_micros", CALL_ESTIMATE_USD_MICROS))
    read = reader or repo_reader(connection, settings, project_id, actor)
    names = {u.key: u.name for u in units}
    focus = " ".join(str((inputs or {}).get("focus") or "").split())[:500]
    saved = get_llm_context()
    report: dict[str, Any] = {
        "scan_id": scan_id, "scope": list(parsed.keys), "scope_words": parsed.words(),
        "limits": {"max_files": limits.max_files, "max_rules": limits.max_rules,
                   "budget_usd": limits.budget_usd_micros / 1_000_000},
        "notes": list(limits.notes) + ([] if exclude_tests else ["test code read too "
                                                                 "(exclude_tests false)"]),
        "units_in_scope": len(units), "engine": engine,
        "files_read": 0, "classes_read": 0, "calls": 0, "rules_proposed": 0, "spend": 0.0,
        "stopped_by": "scope_exhausted", "proposals": [], "set_aside": [], "unread": [],
        "malformed_answers": 0, "provenance": {"agent_role": agent_role,
                                               "agent_name": agent_name or None,
                                               "workflow_run_id": workflow_run_id or None,
                                               "step": step_id, "scan_id": scan_id},
    }
    spent = 0
    known_packages: dict[str, bool] = {}
    files: set[str] = set()
    detail = ""
    set_llm_context(project_id=project_id, run_id=workflow_run_id or scan_id, stage=STAGE,
                    role_key=agent_role, workflow_run_id=workflow_run_id or None,
                    db_engine=getattr(connection, "engine", None))
    try:
        for unit in walk_order(units):
            if report["rules_proposed"] >= limits.max_rules:
                report["stopped_by"] = "max_rules"
                break
            if unit.path not in files and len(files) >= limits.max_files:
                report["stopped_by"] = "max_files"
                break
            if spent + estimate > limits.budget_usd_micros:
                report["stopped_by"] = "budget"
                break
            if keep_claim is not None and not keep_claim():
                report["stopped_by"] = "claim_lost"
                break
            start = max(1, unit.line or 1)
            end = min(max(unit.end_line, start), start + MAX_LINES - 1)
            got = read(unit.path, start, end, unit.repo)
            if got.get("error") or not str(got.get("content") or "").strip():
                report["unread"].append({"path": unit.path, "unit": unit.name,
                                         "reason": str(got.get("error") or "empty")})
                continue
            content = str(got["content"])[:MAX_CHARS]
            first = _num(got.get("start"), start)
            code, held = _numbered(content, first)
            files.add(unit.path)
            report["files_read"] = len(files)
            report["classes_read"] += 1
            remaining = limits.max_rules - report["rules_proposed"]
            prompt = scan_prompt(unit, code, start=first, end=first + len(held) - 1,
                                 facts=unit.facts(names), remaining=remaining,
                                 scope_words=parsed.words(), focus=focus)
            try:
                held_budget = governor.reserve()
            except BudgetRefused as refused:
                report["stopped_by"], detail = "budget_refused", str(refused)[:300]
                break
            try:
                with tempfile.TemporaryDirectory(prefix="boss-rule-scan-") as cwd:
                    result = agent.run_role(prompt, Path(cwd), read_only=True,
                                            max_turns=MAX_TURNS) or {}
            except Exception as error:  # noqa: BLE001 — said in the report; what was proposed stays
                governor.fail(held_budget, error)
                from ..redaction import redact_secrets

                report["stopped_by"] = "engine_failed"
                detail = f"{type(error).__name__}: {redact_secrets(str(error))}"[:300]
                break
            report["calls"] += 1
            usage = _usage_of(result)
            cost = int(governor.settle(held_budget, usage) or 0)
            if result.get("cost_unreported") and not cost and estimate:
                # An engine that does not report its cost is charged its reservation here,
                # never counted free (engine contract: cost_unreported).
                cost = estimate
            spent += cost
            report["spend"] = round(spent / 1_000_000, 6)
            model_id = str(usage.get("model") or result.get("model") or "")
            proposals = parse_answer(str(result.get("summary") or ""))
            if proposals is None:
                report["malformed_answers"] += 1
                continue
            ranked = sorted(proposals, key=lambda p: -_confidence(p))
            for proposal in ranked:
                if report["rules_proposed"] >= limits.max_rules:
                    break
                args, why = validate_proposal(
                    unit, proposal, held,
                    package_known=_package_known(connection, project_id, unit, known_packages))
                if args is None:
                    report["set_aside"].append({"unit": unit.name, "path": unit.path,
                                                "title": str(proposal.get("title") or "")[:160],
                                                "reason": why})
                    continue
                # PROVENANCE FROM THE CONTEXT, never from the answer (rules.propose_rule).
                set_llm_context(step=step_id, scan_id=scan_id, knowledge_scan_id=scan_id,
                                agent_name=agent_name or None, model_id=model_id or None)
                try:
                    with connection.begin_nested():
                        rule = rules.propose_rule(connection, project_id=project_id,
                                                  scan_id=scan_id, **args)
                except Exception as refused:  # noqa: BLE001 — a refusal in words, kept
                    report["set_aside"].append({"unit": unit.name, "path": unit.path,
                                                "title": args["title"],
                                                "reason": str(refused)[:300]})
                    continue
                report["rules_proposed"] += 1
                report["proposals"].append(_proposal_row(rule, args, unit))
        else:
            report["stopped_by"] = "scope_exhausted"
        if report["stopped_by"] == "scope_exhausted" and report["rules_proposed"] >= \
                limits.max_rules:
            report["stopped_by"] = "max_rules"
    finally:
        from ..llm_telemetry import clear_llm_context

        clear_llm_context()
        if saved:
            set_llm_context(**saved)
    report["stopped_reason"] = _STOP_WORDS[report["stopped_by"]].format(
        max_files=limits.max_files, max_rules=limits.max_rules,
        budget=limits.budget_usd_micros / 1_000_000, detail=detail or "no reason given")
    report["summary"] = summary_words(report)
    return report


def _package_known(connection: Connection, project_id: str, unit: Unit,
                   cache: dict[str, bool]) -> bool:
    """Whether a rule's scope may name the unit's package: only one the graph has a module node
    for (rules._package_node refuses any other). A package the tree derived from a directory
    stays out of the scope; the rule still names its area and symbols."""
    if not unit.package:
        return False
    if unit.package not in cache:
        try:
            cache[unit.package] = connection.execute(
                text("SELECT 1 FROM kg_nodes WHERE project_id = :p AND kind = 'module' "
                     "AND valid_to IS NULL AND name = :n LIMIT 1"),
                {"p": project_id, "n": unit.package}).first() is not None
        except Exception:  # noqa: BLE001 — no graph to ask: leave the package out
            cache[unit.package] = False
    return cache[unit.package]


def _confidence(proposal: Mapping[str, Any]) -> float:
    try:
        return float(proposal.get("confidence"))
    except (TypeError, ValueError):
        return 0.0


def _proposal_row(rule: Any, args: Mapping[str, Any], unit: Unit) -> dict[str, Any]:
    rule = rule if isinstance(rule, Mapping) else {}
    node = rule.get("node") if isinstance(rule.get("node"), Mapping) else {}
    return {"rule_id": node.get("id") or rule.get("id"), "title": args["title"],
            "category": args["category"], "confidence": args["confidence"],
            "unit": unit.name, "path": unit.path,
            "symbols": list(args["scope"]["symbols"]),
            "status": rule.get("status") or "proposed"}


def summary_words(report: Mapping[str, Any]) -> str:
    rules_n, files_n = int(report.get("rules_proposed") or 0), int(report.get("files_read") or 0)
    words = (f"Proposed {rules_n} rule{'s' if rules_n != 1 else ''} from {files_n} "
             f"file{'s' if files_n != 1 else ''} in {report.get('scope_words')}, for "
             f"${float(report.get('spend') or 0):.2f}. Stopped because "
             f"{report.get('stopped_reason')}.")
    if rules_n:
        words += " A person approves or rejects each one in Graph › Rules review."
    return words


# ── The workflow steps ──────────────────────────────────────────────────────────────────────

def _step_claim(connection: Connection, project_id: str, run_id: str, step_id: str
                ) -> Optional[Callable[[], bool]]:
    """Renew this step's claim between calls (a scan outlasts one claim), fenced on the claim
    the step holds now — the architecture step's renewal, shared."""
    if not run_id:
        return None
    row = connection.execute(
        text("SELECT id, lease_owner, attempt FROM workflow_step_runs "
             "WHERE workflow_run_id = :r AND project_id = :p AND step_id = :s "
             "AND status IN ('leased', 'running') ORDER BY iteration DESC LIMIT 1"),
        {"r": run_id, "p": project_id, "s": step_id},
    ).mappings().first()
    if row is None or not row["lease_owner"]:
        return None
    from ..architecture_docs import _renew_step_lease

    return lambda: _renew_step_lease(connection, row["id"], row["lease_owner"], row["attempt"])


def run_step(connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
             context: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    """The ``scan_business_rules`` workflow step. Raises RuleScanRefused in words."""
    run_id = str(context.get("run_id") or "")
    step_id = str(step.get("id") or "scan")
    role = str(step.get("captain") or "").strip().lower()
    if not role or role == "human":
        role = DEFAULT_ROLE
    from ..model_routing import routed_settings

    report = scan(
        connection, routed_settings(connection, settings, project_id),
        project_id=project_id, scope=inputs.get("scope"), inputs=inputs,
        scan_id=f"rscan_{run_id[-12:]}_{step_id}"[:64] if run_id else None,
        agent_role=role, agent_name=_agent_name(connection, project_id, role),
        workflow_run_id=run_id, step_id=step_id,
        actor=str(context.get("actor") or "workflow_runner"),
        keep_claim=_step_claim(connection, project_id, run_id, step_id))
    return {"capability": CAPABILITY, "tool": None, "status": "succeeded",
            "scan_id": report["scan_id"], "files_read": report["files_read"],
            "rules_proposed": report["rules_proposed"], "spend_usd": report["spend"],
            "stopped_by": report["stopped_by"], "report": report, "note": report["summary"],
            "review_link": review_link(report["scan_id"])}


def _agent_name(connection: Connection, project_id: str, role: str) -> str:
    try:
        row = connection.execute(
            text("SELECT display_name FROM roles WHERE project_id = :p AND role_key = :k "
                 "LIMIT 1"), {"p": project_id, "k": role}).first()
    except Exception:  # noqa: BLE001 — a name is a nicety
        return ""
    return str(row[0]) if row and row[0] else ""


def review_link(scan_id: str) -> dict[str, Any]:
    """Where the review happens: Graph › Rules review, filtered to this scan."""
    return {"area": "knowledge", "view": "rules", "status": "proposed", "scan_id": scan_id,
            "label": "Open Rules review"}


def _scan_id_of(context: Mapping[str, Any]) -> str:
    for produced in (context.get("steps") or {}).values():
        if isinstance(produced, Mapping) and produced.get("scan_id"):
            return str(produced["scan_id"])
    return ""


def open_proposals(connection: Connection, project_id: str, scan_id: str) -> int:
    """How many of this scan's rules still wait for a person."""
    if not scan_id:
        return 0
    return int(connection.execute(
        text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind = 'rule' "
             "AND valid_to IS NULL AND COALESCE(attrs->>'status', 'proposed') = 'proposed' "
             "AND COALESCE(attrs->>'scan_id', attrs->'provenance'->>'scan_id') = :s"),
        {"p": project_id, "s": scan_id},
    ).scalar() or 0)


def has_open_proposals(connection: Connection, project_id: str,
                       context: Optional[Mapping[str, Any]]) -> bool:
    """Whether the review gate has anything to ask: with an empty queue it steps aside."""
    return open_proposals(connection, project_id, _scan_id_of(context or {})) > 0


def not_applicable(context: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    scan_id = _scan_id_of(context or {})
    return {"capability": REVIEW_CAPABILITY, "tool": None, "status": "not_applicable",
            "scan_id": scan_id or None,
            "note": "Nothing to review: the scan has no proposed rule waiting for a person."}


def review_card(connection: Connection, project_id: str, context: Mapping[str, Any]
                ) -> dict[str, Any]:
    """What the review gate's decision carries beside its action (REVIEW_ACTION, set where the
    gate opens): the scan, how many rules wait, and the link to Rules review."""
    scan_id = _scan_id_of(context)
    return {"scan_id": scan_id or None,
            "proposed_rules": open_proposals(connection, project_id, scan_id),
            "link": review_link(scan_id)}


def settle_empty_reviews(connection: Connection, project_id: str, run_id: str) -> list[str]:
    """Answer this run's waiting rules-review gates whose scan has nothing left to review (the
    runner calls it before it reads answered gates). Returns the decisions it settled."""
    from sqlalchemy import bindparam

    from ..decision_actions import OPEN_STATUSES

    rows = connection.execute(
        text("SELECT d.recommendation->>'scan_id' AS scan_id FROM workflow_step_runs s "
             "JOIN decisions d ON d.id = s.decision_id WHERE s.workflow_run_id = :r "
             "AND s.status = 'waiting' AND d.status IN :open "
             "AND d.recommendation->>'action' = :a").bindparams(
            bindparam("open", expanding=True)),
        {"r": run_id, "open": sorted(OPEN_STATUSES), "a": REVIEW_ACTION},
    ).all()
    settled = []
    for (scan_id,) in rows:
        done = settle_review_gate(connection, None, project_id, str(scan_id or ""))
        if done:
            settled.append(done)
    return settled


def settle_review_gate(connection: Connection, settings: Any, project_id: str, scan_id: str
                       ) -> Optional[str]:
    """Approve the scan's open review gate once its queue is empty (every rule approved or
    rejected). The runner calls it each tick (settle_empty_reviews); a review action may call
    it at once. Returns the decision it settled, or None."""
    if not scan_id or open_proposals(connection, project_id, scan_id):
        return None
    from sqlalchemy import bindparam

    from ..decision_actions import OPEN_STATUSES

    row = connection.execute(
        text("SELECT id FROM decisions WHERE project_id = :p AND status IN :open "
             "AND recommendation->>'action' = :a "
             "AND recommendation->>'scan_id' = :s ORDER BY created_at LIMIT 1"
             ).bindparams(bindparam("open", expanding=True)),
        {"p": project_id, "a": REVIEW_ACTION, "s": scan_id, "open": sorted(OPEN_STATUSES)},
    ).first()
    if row is None:
        return None
    from ..decisions import SYSTEM_ACTOR, DecisionService

    if settings is None:
        from ...config import get_settings

        settings = get_settings()
    DecisionService(settings).transition(
        connection, str(row[0]), action="approve", actor=SYSTEM_ACTOR, system_policy=True,
        note="Every rule this scan proposed has been approved or rejected in Rules review.")
    return str(row[0])
