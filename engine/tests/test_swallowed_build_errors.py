"""TWO REAL ERRORS EVERY BUILD SWALLOWED, AND THE CLASS EACH BELONGS TO.

1. `AttributeError: 'NoopBudgetPort' object has no attribute 'status_for_project'` (x57 in one
   laptop's app_logs). The governed pipeline read its budget state through `ports.budget`, a
   BudgetPort, but the protocol had no `status_for_project` and neither adapter implemented it,
   so every build routed as "green" without knowing. `_release` had the same bug with
   `consume` (not on the port), so a failed run's real spend was released unrecorded.
   Class: a method called on a BudgetPort holder that the protocol does not declare.

2. `ProgrammingError ... to_jsonb(:s::text)` in `_mark_stage_done` (x40). SQLAlchemy's text()
   does not read `:s::text` as a bind parameter, so Postgres saw a syntax error on every stage,
   and a resume could not see which stages were already done. Class: a `:name::type` cast in
   SQL text.

3. Both were caught by a broad `except` and logged at WARNING. A deliberately swallowed error
   is now logged at ERROR with the run id and recorded as a `code_task.swallowed_error` event,
   which the build profile counts.
"""
from __future__ import annotations

import ast
import inspect
import json
import logging
import re
from pathlib import Path
from typing import Optional
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.defaults.budget import NoopBudgetPort
from boss_engine.ports.budget import BudgetPort

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRS = (ROOT / "engine" / "boss_engine", ROOT / "apps" / "api" / "app")


def _sources():
    for base in SOURCE_DIRS:
        for path in sorted(base.rglob("*.py")):
            yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# ── 1. Every method called on a BudgetPort is on the protocol ─────────────────────────────────

_PORT_FACTORIES = {"_engine_ports", "get_engine_ports", "oss_defaults"}


def _protocol_methods() -> set[str]:
    return {n for n, v in vars(BudgetPort).items() if callable(v) and not n.startswith("_")}


def _is_ports(node: ast.AST) -> bool:
    """`ports`, `resolved_ports`, `_engine_ports()`, `EnginePorts.oss_defaults(...)`."""
    if isinstance(node, ast.Name):
        return node.id.endswith("ports")
    if isinstance(node, ast.Call):
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        return name in _PORT_FACTORIES
    return False


def _is_port_budget(node: ast.AST) -> bool:
    if isinstance(node, ast.BoolOp):
        return any(_is_port_budget(v) for v in node.values)
    return isinstance(node, ast.Attribute) and node.attr == "budget" and _is_ports(node.value)


def _key(node: ast.AST):
    """A holder's identity: a local name, or `self.<attr>`."""
    if isinstance(node, ast.Name):
        return node.id
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self"):
        return f"self.{node.attr}"
    return None


def _calls_on(scope: ast.AST, holders: set) -> list[tuple[str, int]]:
    out = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            v = node.func.value
            if _is_port_budget(v) or (_key(v) is not None and _key(v) in holders):
                out.append((node.func.attr, node.lineno))
    return out


#: The module each module-level function is defined in (its file stem), and for each module
#: the names it imports `from .<module> import name`.
_MODULE_OF: dict[int, str] = {}
_IMPORTS: dict[str, dict[str, str]] = {}


def _callee(call: ast.Call) -> tuple[Optional[str], Optional[str]]:
    """(function, module) for `f(...)` (any module) or `mod.f(...)` / `_mod.f(...)`, a helper
    module imported by name such as `_build_budget` (that module only)."""
    if isinstance(call.func, ast.Name):
        return call.func.id, None
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        return call.func.attr, call.func.value.id.lstrip("_")
    return None, None


def _followed(fn, held: set, rel: str, functions: dict, seen: set,
              here: Optional[str] = None) -> list[tuple[str, str, int]]:
    """Calls on the holder inside every module-level function it is passed to, TRANSITIVELY:
    `_release(budget, …)` hands it on to build_budget.settle_or_release, which settles it."""
    out: list[tuple[str, str, int]] = []
    for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
        name, module = _callee(call)
        if name not in functions:
            continue
        for i, arg in enumerate(call.args):
            if not (_is_port_budget(arg) or (isinstance(arg, ast.Name) and arg.id in held)):
                continue
            # `mod.f` is that module's f; a bare `f` is this module's own f, or the f it
            # imports (names repeat across modules: app_alerts' `_release(connection, …)` is
            # not code_task_service's `_release(budget, …)`).
            if module is None:
                module = here if any(_MODULE_OF.get(id(t)) == here for t in functions[name]) \
                    else _IMPORTS.get(here or "", {}).get(name)
            for target in functions[name]:
                if module is not None and _MODULE_OF.get(id(target)) != module:
                    continue
                params = [a.arg for a in target.args.args]
                if i >= len(params) or (name, i) in seen:
                    continue
                inner = {params[i]}
                out += [(m, f"{rel} -> {target.name}", ln) for m, ln in _calls_on(target, inner)]
                out += _followed(target, inner, rel, functions, seen | {(name, i)},
                                 _MODULE_OF.get(id(target)))
    return out


def budget_port_call_sites() -> list[tuple[str, str, int]]:
    """(method, file, line) for every call made on something that holds `ports.budget`,
    following a holder passed positionally into a module-level function (`_release(budget, …)`)."""
    trees = list(_sources())
    functions: dict[str, list[ast.FunctionDef]] = {}
    for source, tree in trees:
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                functions.setdefault(node.name, []).append(node)
                _MODULE_OF[id(node)] = source.stem
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    _IMPORTS.setdefault(source.stem, {})[alias.asname or alias.name] = \
                        node.module.rsplit(".", 1)[-1]
    sites: list[tuple[str, str, int]] = []
    for path, tree in trees:
        rel = str(path.relative_to(ROOT))
        scopes: list[tuple[ast.AST, set]] = []
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            held = {_key(t) for n in ast.walk(cls) if isinstance(n, ast.Assign)
                    and _is_port_budget(n.value) for t in n.targets
                    if (_key(t) or "").startswith("self.")}
            if held:
                scopes.append((cls, held))
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            held = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign) and _is_port_budget(n.value)
                    for t in n.targets if isinstance(t, ast.Name)}
            scopes.append((fn, held))
            sites += _followed(fn, held, rel, functions, set(), path.stem)
        for scope, held in scopes:
            sites += [(m, rel, ln) for m, ln in _calls_on(scope, held)]
    return sites


def test_the_scan_sees_the_known_call_sites():
    """The scanner is only worth something if it finds what is known to be there."""
    found = {(m, f.split(" ->")[0]) for m, f, _ in budget_port_call_sites()}
    assert ("status_for_project", "engine/boss_engine/services/governed_code_task_service.py") in found
    assert ("reserve", "engine/boss_engine/services/code_task_service.py") in found
    # via _release -> build_budget.settle_or_release (followed transitively)
    assert ("settle", "engine/boss_engine/services/governed_code_task_service.py") in found
    assert ("extend", "engine/boss_engine/services/governed_code_task_service.py") in found
    assert ("assert_active_reservation", "engine/boss_engine/services/software_factory.py") in found


def test_every_method_called_on_a_budget_port_is_on_the_protocol():
    declared = _protocol_methods()
    missing = sorted({f"{m}  ({f}:{ln})" for m, f, ln in budget_port_call_sites()
                      if m not in declared})
    assert not missing, ("called on ports.budget but not declared by BudgetPort — the OSS "
                         "NoopBudgetPort raises AttributeError there:\n  " + "\n  ".join(missing))


def _implementations():
    impls = [NoopBudgetPort]
    try:
        from apps.api.app.adapters_postgres.budget import PostgresBudgetPort
    except Exception:  # noqa: BLE001 — a Community tree without the API package
        PostgresBudgetPort = None
    if PostgresBudgetPort is not None:
        impls.append(PostgresBudgetPort)
    return impls


@pytest.mark.parametrize("impl", _implementations(), ids=lambda c: c.__name__)
def test_every_budget_port_implements_every_protocol_method(impl):
    for name in _protocol_methods():
        assert callable(getattr(impl, name, None)), f"{impl.__name__} lacks {name}"
        want = list(inspect.signature(getattr(BudgetPort, name)).parameters)
        have = list(inspect.signature(getattr(impl, name)).parameters)
        assert have[:len(want)] == want, f"{impl.__name__}.{name}{have} != protocol {want}"


def test_the_oss_budget_port_reports_a_state_the_router_reads():
    assert NoopBudgetPort().status_for_project(None, "p")["budget_state"] == "green"
    assert isinstance(NoopBudgetPort(), BudgetPort)


# ── 2. No `:name::type` cast in SQL text ──────────────────────────────────────────────────────

_SQL = re.compile(r"\b(SELECT|UPDATE|INSERT|DELETE)\b", re.I)
_COLON_CAST = re.compile(r"(?<![:\w]):[A-Za-z_]\w*::")


def test_no_text_sql_uses_a_colon_cast():
    """`:s::text` is not a bind parameter to text() — Postgres receives the literal `:s` and
    fails. Write CAST(:s AS text)."""
    bad = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                    _SQL.search(node.value) and _COLON_CAST.search(node.value)):
                bad.append(f"{path.relative_to(ROOT)}:{node.lineno}: "
                           f"{_COLON_CAST.search(node.value).group(0)}")
    assert not bad, "use CAST(:name AS type), not :name::type:\n  " + "\n  ".join(bad)


def test_the_colon_cast_check_catches_the_bug_it_names():
    assert _COLON_CAST.search("|| to_jsonb(:s::text))")
    assert not _COLON_CAST.search("'[]'::jsonb || to_jsonb(CAST(:s AS text)) WHERE id=:id")


# ── Against the test database ─────────────────────────────────────────────────────────────────

def _seed_run(eng) -> tuple[str, str]:
    from ._obs_seed import project

    run_id = f"crun_{uuid4().hex[:20]}"
    with eng.begin() as c:
        pid = project(c, "p_swallowed")
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status) "
                       "VALUES (:r, :p, 'mark me', 'running')"), {"r": run_id, "p": pid})
    return pid, run_id


@pytest.fixture()
def pg(monkeypatch):
    from ._obs_seed import pg_engine
    from boss_engine.services import governed_code_task_service as governed

    eng = pg_engine()
    monkeypatch.setattr(governed, "_transaction", lambda e: e.begin())
    return eng


def _swallowed_events(eng, run_id):
    from boss_engine.services.crash_frames import SWALLOWED_EVENT

    with eng.connect() as c:
        return [d if isinstance(d, dict) else json.loads(d) for d in c.execute(text(
            "SELECT data FROM events WHERE entity_id = :r AND event_type = :k"),
            {"r": run_id, "k": SWALLOWED_EVENT}).scalars().all()]


def test_mark_stage_done_records_each_stage_for_a_resume(pg):
    from boss_engine.services.governed_code_task_service import mark_stage_done

    pid, run_id = _seed_run(pg)
    assert mark_stage_done(pg, pid, run_id, "design") is True
    assert mark_stage_done(pg, pid, run_id, "write_tests") is True
    with pg.connect() as c:
        fctx = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:r"),
                         {"r": run_id}).scalar()
    fctx = fctx if isinstance(fctx, dict) else json.loads(fctx)
    assert fctx["completed_stages"] == ["design", "write_tests"]
    assert _swallowed_events(pg, run_id) == []


def test_a_swallowed_failure_is_an_error_with_the_run_id_and_is_counted(pg, monkeypatch, caplog):
    from boss_engine.services import governed_code_task_service as governed
    from boss_engine.services.build_profile import compute_profile

    pid, run_id = _seed_run(pg)
    monkeypatch.setattr(governed, "sql", lambda s: text("UPDATE no_such_table SET x = :s WHERE 1=:id"))
    with caplog.at_level(logging.WARNING, logger=governed.logger.name):
        assert governed.mark_stage_done(pg, pid, run_id, "implement") is False   # never raises
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and run_id in errors[0].getMessage()
    assert "mark_stage_done:implement" in errors[0].getMessage()
    events = _swallowed_events(pg, run_id)
    assert [(e["where"], e["error"]) for e in events] == [("mark_stage_done:implement",
                                                           "ProgrammingError")]
    with pg.connect() as c:
        profile = compute_profile(run_id, c)
    assert profile["swallowed_errors"] == {
        "count": 1, "by_where": {"mark_stage_done:implement: ProgrammingError": 1}}


def test_release_settles_real_spend_through_the_port(pg):
    """`_release` called `consume`, which no BudgetPort has: the spend of a failed run was
    released unrecorded. Through the port's `settle`, the OSS reservation is consumed."""
    from boss_engine.services.code_task_service import _release

    pid, run_id = _seed_run(pg)
    port = NoopBudgetPort()
    res = port.reserve(None, pid, "code_task", 500_000, "build", 60)["reservation_id"]
    with pg.begin() as c:
        c.execute(text("INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, "
                       "cost_usd_micros, included_in_subscription) "
                       "VALUES (:id, :p, :r, 'implement', 1234, false)"),
                  {"id": f"llmc_{uuid4().hex}", "p": pid, "r": run_id})
        _release(port, c, pid, res, "tests did not pass", run_id=run_id)
    assert port._reservations[res]["status"] == "consumed"
    assert _swallowed_events(pg, run_id) == []
