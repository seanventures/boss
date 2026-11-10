"""The benchmark suite: load it, validate it, pin it.

A suite is a YAML file (bench/harness/suite.yaml). Every directory it names (fixtures, held
acceptance tests, scripted solutions) is hashed into the suite's sha256, and every fixture carries
its own pinned sha256 in the file: a fixture edited without re-pinning is refused, and a baseline
made on another suite hash cannot be compared with (compare.py).
"""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SUITE_DIR = REPO_ROOT / "bench" / "harness"
DEFAULT_SUITE = SUITE_DIR / "suite.yaml"
BASELINES_DIR = SUITE_DIR / "baselines"

KINDS = ("fix", "feature", "new_app")
#: The faults the scripted engine plays (services/scripted_engine.py).
SCRIPTED_FAULTS = ("failing_test", "blocker", "crash_on_start", "crash")
#: Metrics a suite may configure (metrics.METRICS is the authority; kept here to validate early).
METRIC_NAMES = ("success_rate", "wall_s", "tokens_in", "tokens_out", "cache_share", "cost_usd",
                "model_calls", "turns", "repair_rounds", "interventions")
GUARD_KEYS = ("max_drop", "max_rise", "max_rise_pct")

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
#: What a tree hash and a seeded repository leave out: build and cache litter, never content.
_SKIP_PARTS = {"__pycache__", "node_modules", ".git", ".pytest_cache"}
_SKIP_NAMES = {".DS_Store"}


class SuiteError(ValueError):
    """The suite file is wrong; the message lists every problem found."""


def _skipped(rel: Path) -> bool:
    return (any(p in _SKIP_PARTS for p in rel.parts) or rel.name in _SKIP_NAMES
            or rel.suffix in (".pyc", ".pyo"))


def tree_files(root: Path) -> dict[str, bytes]:
    """{posix relative path: bytes} for every file under `root`, litter left out."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and not _skipped(rel):
            out[rel.as_posix()] = path.read_bytes()
    return out


def tree_sha256(root: Path) -> str:
    """A content hash of a directory: paths and bytes, in path order (modes and times ignored)."""
    h = hashlib.sha256()
    for rel, data in tree_files(root).items():
        h.update(rel.encode() + b"\0" + hashlib.sha256(data).hexdigest().encode() + b"\n")
    return h.hexdigest()


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO_ROOT)
        return True
    except ValueError:
        return False


def _dir(base: Path, rel: Any, what: str, errors: list[str]) -> Optional[Path]:
    if not isinstance(rel, str) or not rel.strip():
        errors.append(f"{what}: a path is required")
        return None
    path = (base / rel).resolve()
    if not _inside_repo(path):
        errors.append(f"{what}: {rel} is outside the repository")
        return None
    if not path.exists():
        errors.append(f"{what}: {rel} does not exist")
        return None
    return path


def _str_list(value: Any, what: str, errors: list[str], *, required: bool) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value) \
            or (required and not value):
        errors.append(f"{what}: a {'non-empty ' if required else ''}list of strings is required")
        return []
    return list(value)


def _check_rules(cfg: Any, where: str, errors: list[str]) -> None:
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        errors.append(f"{where}: a mapping is required")
        return
    for name, rule in (cfg.get("metrics") or {}).items():
        if name not in METRIC_NAMES:
            errors.append(f"{where}.metrics: unknown metric {name!r}")
        elif not isinstance(rule, dict) or any(
                k not in ("abs_floor", "rel_floor") or not isinstance(v, (int, float)) or v < 0
                for k, v in rule.items()):
            errors.append(f"{where}.metrics.{name}: only abs_floor/rel_floor, non-negative numbers")
    for name, rule in (cfg.get("guards") or {}).items():
        if name not in METRIC_NAMES:
            errors.append(f"{where}.guards: unknown metric {name!r}")
        elif rule is None:
            continue   # an override that turns an inherited guard off
        elif not isinstance(rule, dict) or len(rule) != 1 or any(
                k not in GUARD_KEYS or not isinstance(v, (int, float)) or v < 0
                for k, v in rule.items()):
            errors.append(f"{where}.guards.{name}: exactly one of {GUARD_KEYS}, a non-negative number")


def validate(raw: Any, base: Path) -> list[str]:
    """Every problem with a parsed suite, as sentences. An empty list is a valid suite."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["the suite must be a mapping"]
    if not isinstance(raw.get("suite"), str) or not raw["suite"].strip():
        errors.append("suite: a name is required")
    if not isinstance(raw.get("version"), str) or not raw["version"].strip():
        errors.append("version: a string is required (quote it: version: \"1\")")
    noise = raw.get("noise") or {}
    if not isinstance(noise, dict) or not isinstance(noise.get("k_stdev", 2.0), (int, float)) \
            or noise.get("k_stdev", 2.0) < 0:
        errors.append("noise.k_stdev: a non-negative number is required")
    _check_rules({"metrics": raw.get("metrics"), "guards": raw.get("guards")}, "suite", errors)
    if not raw.get("guards"):
        errors.append("guards: at least one guard is required (a benchmark that cannot fail is not a gate)")
    for engine, cfg in (raw.get("engine_overrides") or {}).items():
        _check_rules(cfg, f"engine_overrides.{engine}", errors)

    fixtures = raw.get("fixtures")
    if not isinstance(fixtures, dict) or not fixtures:
        errors.append("fixtures: at least one fixture is required")
        fixtures = {}
    for name, fx in fixtures.items():
        if not isinstance(fx, dict):
            errors.append(f"fixtures.{name}: a mapping is required")
            continue
        path = _dir(base, fx.get("path"), f"fixtures.{name}.path", errors)
        pinned = fx.get("sha256")
        if not isinstance(pinned, str) or not re.fullmatch(r"[0-9a-f]{64}", pinned or ""):
            errors.append(f"fixtures.{name}.sha256: the fixture's pinned tree hash is required")
        elif path is not None and tree_sha256(path) != pinned:
            errors.append(f"fixtures.{name}: its files changed since they were pinned "
                          f"(sha256 {tree_sha256(path)} != {pinned}); re-pin it and bump version")

    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errors.append("tasks: at least one task is required")
        tasks = []
    seen: set[str] = set()
    for i, t in enumerate(tasks):
        where = f"tasks[{i}]"
        if not isinstance(t, dict):
            errors.append(f"{where}: a mapping is required")
            continue
        tid = t.get("id")
        if not isinstance(tid, str) or not _ID.fullmatch(tid):
            errors.append(f"{where}.id: a lower-case slug is required")
        elif tid in seen:
            errors.append(f"{where}.id: {tid} is used twice")
        else:
            seen.add(tid)
            where = f"tasks.{tid}"
        if t.get("kind") not in KINDS:
            errors.append(f"{where}.kind: one of {KINDS}")
        if t.get("fixture") not in fixtures:
            errors.append(f"{where}.fixture: {t.get('fixture')!r} is not a listed fixture")
        for key in ("title", "instructions"):
            if not isinstance(t.get(key), str) or not t[key].strip():
                errors.append(f"{where}.{key}: text is required")
        _str_list(t.get("acceptance_criteria"), f"{where}.acceptance_criteria", errors, required=True)
        _str_list(t.get("test_commands"), f"{where}.test_commands", errors, required=True)
        acc = t.get("acceptance")
        if not isinstance(acc, dict):
            errors.append(f"{where}.acceptance: the benchmark's own checks are required")
        else:
            _dir(base, acc.get("hidden"), f"{where}.acceptance.hidden", errors)
            for inc in _str_list(acc.get("include"), f"{where}.acceptance.include", errors,
                                 required=False):
                path = _dir(base, inc, f"{where}.acceptance.include", errors)
                if path is not None and not path.is_file():
                    errors.append(f"{where}.acceptance.include: {inc} is not a file")
            _str_list(acc.get("commands"), f"{where}.acceptance.commands", errors, required=True)
            changed = _str_list(acc.get("expect_changed"), f"{where}.acceptance.expect_changed",
                                errors, required=False)
            files = _str_list(acc.get("expect_files"), f"{where}.acceptance.expect_files",
                              errors, required=False)
            if not changed and not files:
                errors.append(f"{where}.acceptance: name expect_changed or expect_files")
        scripted = t.get("scripted")
        if not isinstance(scripted, dict):
            errors.append(f"{where}.scripted: the scripted engine's solution is required")
        else:
            _dir(base, scripted.get("solution"), f"{where}.scripted.solution", errors)
            for fault in _str_list(scripted.get("faults"), f"{where}.scripted.faults", errors,
                                   required=False):
                if fault not in SCRIPTED_FAULTS:
                    errors.append(f"{where}.scripted.faults: unknown fault {fault!r}")
    return errors


def load_suite(path: Optional[Path] = None) -> dict[str, Any]:
    """The parsed, validated suite, with `_path`, `_base` and `_sha256` added. Raises SuiteError."""
    path = Path(path or DEFAULT_SUITE).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    errors = validate(raw, path.parent)
    if errors:
        raise SuiteError(f"{path}: " + "; ".join(errors))
    suite = copy.deepcopy(raw)
    suite["_path"] = str(path)
    suite["_base"] = str(path.parent)
    suite["_sha256"] = suite_sha256(suite)
    return suite


def resolve(suite: dict[str, Any], rel: str) -> Path:
    return (Path(suite["_base"]) / rel).resolve()


def suite_sha256(suite: dict[str, Any]) -> str:
    """The suite file's bytes and the tree hash of every directory or file it names."""
    base = Path(suite["_base"]) if "_base" in suite else SUITE_DIR
    h = hashlib.sha256()
    h.update(Path(suite.get("_path") or DEFAULT_SUITE).read_bytes())
    names: set[str] = set()
    for fx in (suite.get("fixtures") or {}).values():
        names.add(fx["path"])
    for t in suite.get("tasks") or []:
        acc = t.get("acceptance") or {}
        names.add(acc.get("hidden"))
        names.update(acc.get("include") or [])
        names.add((t.get("scripted") or {}).get("solution"))
    for rel in sorted(n for n in names if n):
        p = (base / rel).resolve()
        digest = tree_sha256(p) if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
        h.update(f"{rel}\0{digest}\n".encode())
    return h.hexdigest()


def select_tasks(suite: dict[str, Any], spec: str = "all") -> list[dict[str, Any]]:
    """`all`, or a comma-separated list of task ids (in suite order). Raises SuiteError."""
    tasks = suite["tasks"]
    if not spec or spec.strip() == "all":
        return list(tasks)
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    known = {t["id"] for t in tasks}
    unknown = [w for w in wanted if w not in known]
    if unknown:
        raise SuiteError(f"unknown task id(s): {', '.join(unknown)} (known: {', '.join(sorted(known))})")
    return [t for t in tasks if t["id"] in wanted]


def engine_class(engine: str) -> str:
    """The engine_overrides key an engine falls under: `scripted`, else its harness name."""
    return (engine or "").split(":", 1)[0].strip().lower()


def rules_for(suite: dict[str, Any], engine: str) -> dict[str, Any]:
    """{"k_stdev", "metrics": {name: {abs_floor, rel_floor}}, "guards": {name: {key: limit}}} for
    an engine: the suite's rules with its engine override merged in (a null guard turns it off)."""
    metrics = {k: dict(v or {}) for k, v in (suite.get("metrics") or {}).items()}
    guards = {k: dict(v) for k, v in (suite.get("guards") or {}).items() if v}
    over = (suite.get("engine_overrides") or {}).get(engine_class(engine)) or {}
    for k, v in (over.get("metrics") or {}).items():
        metrics[k] = {**metrics.get(k, {}), **(v or {})}
    for k, v in (over.get("guards") or {}).items():
        if v is None:
            guards.pop(k, None)
        else:
            guards[k] = dict(v)
    return {"k_stdev": float((suite.get("noise") or {}).get("k_stdev", 2.0)),
            "metrics": metrics, "guards": guards}
