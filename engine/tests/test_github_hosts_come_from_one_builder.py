"""Every GitHub host comes from ONE builder, identity/github_urls.py (BOSSAPP-1286).

A GitHub Enterprise Server connection has its own API base (https://<host>/api/v3). A literal
`https://api.github.com` default anywhere else is a path that silently bypasses it — the stack
deploy reader, the branch picker and the PR-understanding read each did. So:

* no engine/API code spells `api.github.com` outside the builder (docstrings and comments may);
* the `https://github.com` web literals still in the code are a RATCHET: a file may lose one,
  never gain one, and no new file may add one. Most that remain name third-party projects
  (scanner homepages) or install-wide links; build new links with github_urls.
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

import pytest

from boss_engine.identity import github_urls as G

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("engine/boss_engine", "apps/api/app", "apps/worker", "apps/egress")
BUILDER = "engine/boss_engine/identity/github_urls.py"
API_LITERAL = re.compile(r"api\.github\.com")
WEB_LITERAL = re.compile(r"https?://github\.com")

#: The web literals left today, per file. Lower a number when you remove one; never raise it.
WEB_LITERALS_LEFT = {
    "apps/api/app/main.py": 1,
    "apps/api/app/routes_templates.py": 1,
    "engine/boss_engine/identity/runs.py": 1,
    "engine/boss_engine/services/ai_sre.py": 1,
    "engine/boss_engine/services/ambient/demo.py": 2,
    "engine/boss_engine/services/cli_harness.py": 1,
    "engine/boss_engine/services/deployment_rollout_engine.py": 1,
    "engine/boss_engine/services/intake_identity.py": 1,
    "engine/boss_engine/services/project_experience/common.py": 1,
    "engine/boss_engine/services/project_experience/new_work.py": 1,
    "engine/boss_engine/services/release_planning.py": 1,
    "engine/boss_engine/services/security_scan.py": 5,
    "engine/boss_engine/services/skill_sources.py": 1,
    "engine/boss_engine/services/turn_cards.py": 4,
    "engine/boss_engine/services/work_plane.py": 1,
}


def _string_literals(path: Path):
    """(line, value) of every string constant that is not a docstring."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant):
            docstrings.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            yield node.lineno, node.value


def _files():
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel != BUILDER and "/tests/" not in rel:
                yield rel, path


def test_no_code_outside_the_builder_spells_the_github_api_host():
    offenders = [f"{rel}:{line}: {value[:80]!r}" for rel, path in _files()
                 for line, value in _string_literals(path) if API_LITERAL.search(value)]
    assert not offenders, ("derive the GitHub API base from the connection with "
                           "identity/github_urls.api_base(conn_api_base):\n" + "\n".join(offenders))


def test_github_web_literals_only_go_down():
    found = Counter(rel for rel, path in _files()
                    for _line, value in _string_literals(path) if WEB_LITERAL.search(value))
    grew = {rel: n for rel, n in found.items() if n > WEB_LITERALS_LEFT.get(rel, 0)}
    assert not grew, ("build GitHub web links with identity/github_urls (repo, pull_request, "
                      f"clone_url, web_base_for) — new literals in: {grew}")


def test_the_guard_sees_a_planted_literal(tmp_path):
    planted = tmp_path / "x.py"
    planted.write_text('"""api.github.com in a docstring is fine."""\nBASE = "https://api.github.com"\n')
    assert [v for _l, v in _string_literals(planted) if API_LITERAL.search(v)] == ["https://api.github.com"]


# ---- the builder ----------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_install_override(monkeypatch):
    monkeypatch.delenv(G.API_BASE_ENV, raising=False)
    monkeypatch.delenv(G.WEB_BASE_ENV, raising=False)


def test_a_connection_base_wins_and_github_com_is_only_the_last_default(monkeypatch):
    assert G.api_base() == "https://api.github.com" and G.api_base("") == G.PUBLIC_API
    assert G.api_base("https://ghe.example/api/v3/") == "https://ghe.example/api/v3"
    monkeypatch.setenv(G.API_BASE_ENV, "https://ghe.install/api/v3")
    assert G.api_base() == "https://ghe.install/api/v3"
    assert G.api_base("https://ghe.example/api/v3") == "https://ghe.example/api/v3"
    assert G.web_base() == "https://ghe.install"


def test_an_install_web_url_alone_derives_its_api(monkeypatch):
    monkeypatch.setenv(G.WEB_BASE_ENV, "https://ghe.install")
    assert G.install_api_base() == "https://ghe.install/api/v3"


@pytest.mark.parametrize("api,graphql,web", [
    ("https://api.github.com", "https://api.github.com/graphql", "https://github.com"),
    ("https://ghe.example/api/v3", "https://ghe.example/api/graphql", "https://ghe.example"),
])
def test_rest_graphql_and_web_go_together(api, graphql, web):
    assert G.graphql_url(api) == graphql and G.web_base_for(api) == web
    assert G.api_base_for_web(web) == api
    assert G.repo("o/r", api) == f"{web}/o/r"
    assert G.pull_request("o/r", 7, api) == f"{web}/o/r/pull/7"
    assert G.clone_url("o/r", api) == f"{web}/o/r.git"


def test_a_ghes_host_joins_the_hosts_a_github_credential_may_reach():
    assert "ghe.example" in G.github_hosts("https://ghe.example/api/v3")
    assert set(G.PUBLIC_HOSTS) <= set(G.github_hosts())


def test_clone_urls_and_the_credential_header_follow_the_host():
    from boss_engine.services.repo_host import clone_url_for, git_access

    assert clone_url_for("github", "o", "r") == "https://github.com/o/r.git"
    assert clone_url_for("github", "o", "r", web_base="https://ghe.example") == \
        "https://ghe.example/o/r.git"
    access = git_access("github", "o", "r", "t0k", web_base="https://ghe.example")
    assert access.url == "https://ghe.example/o/r.git"
    assert "http.https://ghe.example/.extraheader" in access.env.values()
