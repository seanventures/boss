"""P0: ONE answer to "is this production?" (BOSSAPP-1441, dev autonomy).

Nine places used to answer it nine ways (`environment == "production"`, `{"production", "prod"}`,
a `prod|production|live` regex, the incident squad's inverse list...), so a `prod` target was
production to one reader and not to the next. services/production.py is now the only place that
knows the words; everything else calls it.

This fails when code under engine/boss_engine, apps/api/app or apps/worker:
  * compares something environment-like against "production" / "prod" / "prd" / "live" (or a
    tuple/set holding one), or
  * defines its own `is_prod*` / `is_production*` / `_is_prod*` helper, or
  * keeps a module-level set of production names,
outside services/production.py. The resolver's rules are pinned below as well.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from boss_engine.services import production

ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("engine/boss_engine", "apps/api/app", "apps/worker")
RESOLVER = "engine/boss_engine/services/production.py"
PROD_WORDS = {"production", "prod", "prd", "live"}
#: Thin delegates to the resolver, kept for their callers. Each body is one call to it.
DELEGATES = {
    ("engine/boss_engine/services/quality_bar.py", "is_production"),
    ("engine/boss_engine/services/incident_squad.py", "is_prod"),
}
#: Not a production classifier, each with why.
NOT_A_CLASSIFIER = {
    # An AGENT's deployment lanes are an enum (dev | staging | prod) of agent versions, not a
    # place an app ships to (agent platform P3/P8): `environment == "prod"` is the lane's name.
    "engine/boss_engine/services/agent_deployments.py",
    "engine/boss_engine/services/a2a_server.py",
    "engine/boss_engine/services/agent_protocol.py",
    "engine/boss_engine/services/fleet_scheduler.py",
    "engine/boss_engine/services/run_plane.py",
    "apps/api/app/routers/agent_platform.py",
}
#: Module-level names that hold production words for another reason.
NOT_A_NAME_LIST = {
    # Branch names a delegate never merges into (a branch, not an environment).
    ("engine/boss_engine/identity/approval_delegations.py", "PROTECTED_BRANCHES"),
    # The demo-login switch's three labels (screen 13), a display order.
    ("engine/boss_engine/services/project_experience/settings_view.py", "_SWITCH"),
    # Words in a deploy STEP's id ("deploy_prod") when the step names no environment.
    ("engine/boss_engine/services/project_experience/work_stages.py", "_PROD_WORDS"),
}


def _files():
    for base in SCAN_ROOTS:
        root = ROOT / base
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = str(path.relative_to(ROOT))
            if "tests" in path.relative_to(ROOT).parts or rel == RESOLVER \
                    or rel in NOT_A_CLASSIFIER:
                continue
            yield rel, path.read_text(encoding="utf-8")


def _words(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value.strip().lower()}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        out: set[str] = set()
        for elt in node.elts:
            out |= _words(elt)
        return out
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("frozenset", "set") \
            and node.args:
        return _words(node.args[0])
    return set()


def _envish(source: str, node: ast.AST) -> bool:
    text = (ast.get_source_segment(source, node) or "").lower()
    if "environment_tier(" in text or "verdict.tier" in text:
        return False  # the resolver's own answer, compared with its own tier words
    return "env" in text or "tier" in text


def _findings() -> list[str]:
    found = []
    for rel, source in _files():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                sides = [node.left, *node.comparators]
                for i, side in enumerate(sides):
                    if _words(side) & PROD_WORDS:
                        others = sides[:i] + sides[i + 1:]
                        if any(_envish(source, o) for o in others):
                            found.append(f"{rel}:{node.lineno} compares an environment with a "
                                         "production word")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name.lstrip("_").lower()
                if (name.startswith("is_prod") or name.startswith("is_production")) \
                        and (rel, node.name) not in DELEGATES:
                    found.append(f"{rel}:{node.lineno} defines its own {node.name}()")
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {getattr(t, "id", "") for t in targets}
                if any((rel, n) in NOT_A_NAME_LIST for n in names):
                    continue
                if value is not None and len(_words(value) & PROD_WORDS) >= 2:
                    found.append(f"{rel}:{node.lineno} keeps its own set of production names")
    return found


def test_there_is_one_production_resolver():
    found = _findings()
    assert not found, ("Ask services/production.py instead (resolve, resolve_target, "
                       "resolve_merge, is_production_environment, production_affecting, "
                       "risk_tier_for):\n  " + "\n  ".join(found))


def test_the_delegates_only_call_the_resolver():
    for rel, name in DELEGATES:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        source = ast.unparse(fn)
        assert "production" in source and "PRODUCTION_NAMES" not in source


# ---- the rules ----------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["production", "prod", "PRD", "live", "prod-eu", "production2",
                                  "live-b", "prod_west"])
def test_production_names_are_production(name):
    verdict = production.resolve_environment(name, host="203.0.113.7")
    assert verdict.prod and verdict.known


@pytest.mark.parametrize("name, tier", [("staging", "staging"), ("qa", "staging"),
                                        ("preview", "staging"), ("dev", "dev"),
                                        ("development", "dev"), ("staging-eu", "staging"),
                                        ("dev_2", "dev"), ("sandbox", "dev")])
def test_development_and_staging_names_are_not(name, tier):
    verdict = production.resolve_environment(name, host="203.0.113.7")
    assert not verdict.prod and verdict.known and verdict.tier == tier


@pytest.mark.parametrize("name", ["eu1", "customer-a", "main", "", None])
def test_an_unknown_remote_is_production(name):
    verdict = production.resolve_environment(name, host="deploy@203.0.113.7:22")
    assert verdict.prod and not verdict.known and verdict.source == "unknown_remote"
    assert "could not tell" in verdict.words


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "app.localhost", "[::1]:8080",
                                  "boss@host.docker.internal", "http://localhost:3000/x",
                                  "host.lima.internal"])
def test_a_local_install_is_development(host):
    verdict = production.resolve_environment("eu1", host=host)
    assert not verdict.prod and verdict.known and verdict.source == "local_host"


def test_a_set_tier_wins_over_the_name():
    assert not production.target_verdict({"environment": "production", "tier": "staging",
                                          "target_ref": "x@203.0.113.7"}).prod
    assert production.target_verdict({"environment": "dev", "tier": "prod"}).prod
    assert not production.target_verdict({"environment": "eu1", "target_kind": "local"}).prod


def test_the_install_is_development_unless_it_says_production(monkeypatch):
    monkeypatch.delenv("BOSS_INSTALL_TIER", raising=False)
    assert not production.resolve_install().prod
    monkeypatch.setenv("BOSS_INSTALL_TIER", "production")
    assert production.resolve_install().prod and production.install_tier() == "prod"
    monkeypatch.setenv("BOSS_INSTALL_TIER", "something-else")
    verdict = production.resolve_install()
    assert verdict.prod and not verdict.known


def test_public_and_nothing_named_are_production():
    assert production.resolve(None, {"public": True}).prod
    nothing = production.resolve(None, {})
    assert nothing.prod and not nothing.known


def test_production_affecting_and_risk_follow_the_verdict():
    assert production.production_affecting(environment="prod")
    assert production.production_affecting(environment="live")
    assert not production.production_affecting(environment="staging")
    assert production.risk_tier_for("live") == 3 and production.risk_tier_for("staging") == 2


# ---- merges (an in-memory database with the one table the resolver reads) -----------------------

@pytest.fixture()
def repo_db():
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine("sqlite://")
    with engine.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE github_repository_bindings (id TEXT, project_id TEXT, full_name TEXT, "
            "default_branch TEXT, host TEXT, status TEXT, default_branch_ships_to TEXT)")
        c.exec_driver_sql(
            "CREATE TABLE deployment_targets (id TEXT, project_id TEXT, environment TEXT, "
            "tier TEXT, target_kind TEXT, target_ref TEXT, host_id TEXT, status TEXT, "
            "updated_at TEXT)")
        c.exec_driver_sql("CREATE TABLE deployment_hosts (id TEXT, address TEXT)")
        c.exec_driver_sql(
            "INSERT INTO github_repository_bindings VALUES "
            "('b1','p','acme/app','main','github','active',NULL),"
            "('b2','p','acme/shipsnowhere','main','github','active','none'),"
            "('b3','p','acme/toprod','main','github','active','t_prod'),"
            "('b4','p','acme/tostaging','main','github','active','t_stg'),"
            "('b5','p','local_p/app','main','local','active',NULL)")
        c.exec_driver_sql(
            "INSERT INTO deployment_targets VALUES "
            "('t_prod','p','production','prod','ssh_host','x@203.0.113.9',NULL,'active',NULL),"
            "('t_stg','p','staging','staging','ssh_host','x@203.0.113.9',NULL,'active',NULL)")
    with engine.begin() as c:
        yield c


def test_a_merge_into_a_release_branch_is_development(repo_db):
    verdict = production.resolve_merge(repo_db, project_id="p", repo="acme/app",
                                       base_branch="rel/rc11")
    assert not verdict.prod and verdict.known


def test_a_merge_into_a_default_branch_nobody_described_is_production(repo_db):
    verdict = production.resolve_merge(repo_db, project_id="p", repo="acme/app",
                                       base_branch="main")
    assert verdict.prod and not verdict.known


def test_a_merge_follows_where_the_default_branch_ships(repo_db):
    assert production.resolve_merge(repo_db, project_id="p", repo="acme/toprod",
                                    base_branch="main").prod
    assert not production.resolve_merge(repo_db, project_id="p", repo="acme/tostaging",
                                        base_branch="main").prod
    assert not production.resolve_merge(repo_db, project_id="p", repo="acme/shipsnowhere",
                                        base_branch="main").prod


def test_a_local_repository_ships_nowhere(repo_db):
    assert not production.resolve_merge(repo_db, project_id="p", repo="local_p/app",
                                         base_branch="main").prod
