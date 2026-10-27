"""A build clones, and opens its PR against, the PROJECT'S branch — not the GitHub default.

Project boss-platform binds paramg/boss with default_branch 'rel/rc10.10-kg' (the console's
New build form shows it). Its builds cloned `main` — the API wrote "main" when the form sent
no branch — and opened PRs #2/#3 against `main`, the GitHub default (crun_23d665a9,
crun_2f4eb5eb, 2026-10-26). One resolver (services/build_base_branch.py) now answers both, in
order: explicit spec base -> the project's binding -> the host default.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import build_base_branch as bbb
from boss_engine.services.software_factory import _accepts_kwarg

SERVICES = Path(__file__).resolve().parents[1] / "boss_engine" / "services"
API_MAIN = Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "main.py"


@pytest.fixture()
def conn():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE github_repository_bindings (id TEXT, project_id TEXT, full_name TEXT,"
            " role TEXT DEFAULT 'primary', status TEXT DEFAULT 'active', default_branch TEXT)"))
        yield c


def _bind(c, branch, *, project="boss-platform", full_name="paramg/boss", status="active"):
    c.execute(text("INSERT INTO github_repository_bindings (id, project_id, full_name, role,"
                   " status, default_branch) VALUES (:i, :p, :f, 'primary', :s, :b)"),
              {"i": f"b-{project}-{full_name}", "p": project, "f": full_name, "s": status,
               "b": branch})


def _resolve(c, explicit=None, *, host="main", project="boss-platform"):
    calls = []

    def host_default():
        calls.append(1)
        return host
    base = bbb.resolve_build_base(c, project_id=project, owner="paramg", repo="boss",
                                  explicit=explicit, host_default=host_default, run_id="crun_t")
    return base, calls


def test_the_projects_bound_branch_wins_over_the_github_default(conn):
    _bind(conn, "rel/x")
    base, host_calls = _resolve(conn, host="main")
    assert (base.clone, base.pr) == ("rel/x", "rel/x")
    assert base.clone_source == base.pr_source == bbb.SOURCE_BINDING
    assert host_calls == []  # GitHub is not asked when the project already answers


def test_an_explicit_spec_base_wins_over_the_binding(conn):
    _bind(conn, "rel/x")
    base, _ = _resolve(conn, "feature/y", host="main")
    assert (base.clone, base.pr) == ("feature/y", "feature/y")
    assert base.clone_source == bbb.SOURCE_SPEC


def test_no_binding_falls_back_to_the_github_default(conn):
    base, _ = _resolve(conn, host="trunk")
    assert (base.clone, base.pr) == ("trunk", "trunk")
    assert base.pr_source == bbb.SOURCE_HOST_DEFAULT


def test_another_projects_or_a_disabled_binding_does_not_count(conn):
    _bind(conn, "rel/other", project="someone-else")
    _bind(conn, "rel/off", full_name="paramg/BOSS-off", status="disabled")
    base, _ = _resolve(conn, host="main")
    assert base.clone == "main" and base.pr_source == bbb.SOURCE_HOST_DEFAULT


def test_the_binding_matches_the_repo_case_insensitively(conn):
    _bind(conn, "rel/x", full_name="ParamG/Boss")
    assert _resolve(conn)[0].clone == "rel/x"


def test_an_unreadable_host_default_keeps_the_old_main(conn):
    def boom():
        raise RuntimeError("no token")
    base = bbb.resolve_build_base(conn, project_id="p", owner="o", repo="r", host_default=boom)
    assert (base.clone, base.pr_source) == ("main", bbb.SOURCE_FALLBACK)


def test_a_lineage_branch_is_cloned_but_never_a_pr_base(conn):
    """A follow-up builds ON its app's boss/code/<root> branch; its PR still targets the
    project's mainline, never the lineage branch (which is the PR's own head)."""
    _bind(conn, "rel/x")
    base, _ = _resolve(conn, "boss/code/crun_abc", host="main")
    assert base.clone == "boss/code/crun_abc"
    assert (base.pr, base.pr_source) == ("rel/x", bbb.SOURCE_BINDING)


def test_a_mock_adapter_never_becomes_a_branch_name():
    assert bbb.host_default_branch(MagicMock()) is None
    gh = MagicMock()
    gh.default_branch_sha.return_value = ("develop", "sha")
    assert bbb.host_default_branch(gh) == "develop"


def test_a_stored_run_gets_the_same_pr_base(conn):
    _bind(conn, "rel/x")
    run = {"id": "crun_s", "project_id": "boss-platform", "finalize_ctx": {},
           "spec": {"owner": "paramg", "repo": "boss", "base_branch": None}}
    assert bbb.pr_base_for_run(conn, run, None) == "rel/x"
    from boss_engine.services import release_scope
    # A release fix keeps its own base at the serving commit (release_scope).
    release = {**run, "finalize_ctx": {release_scope.FCTX_KEY: {
        "branch": "boss/fix/x", "base_sha": "abc", "pr_base": "boss/serving/x"}}}
    assert bbb.pr_base_for_run(conn, release, None) == "boss/serving/x"


def test_the_pr_base_reaches_only_hosts_that_take_one():
    class GitHubLike:
        def create_pull_request(self, branch_name, title, body, draft=None, base=None): ...

    class BitbucketLike:
        def create_pull_request(self, branch_name, title, body): ...
    assert _accepts_kwarg(GitHubLike().create_pull_request, "base")
    assert not _accepts_kwarg(BitbucketLike().create_pull_request, "base")


# --- wiring: both pipelines and every PR path use the one resolver ------------------------

def test_both_pipelines_resolve_the_base_through_the_one_resolver():
    for name in ("governed_code_task_service.py", "code_task_service.py"):
        source = (SERVICES / name).read_text()
        assert "resolve_build_base(" in source, name
        assert 'spec.get("base_branch") or "main"' not in source, name


def test_every_pr_a_build_opens_names_its_base():
    for name in ("governed_code_task_service.py", "code_task_service.py"):
        source = (SERVICES / name).read_text()
        starts = [m.start() for m in re.finditer(r"sf\.propose_pull_request\(", source)]
        assert starts, name
        for at in starts:
            depth, i = 0, source.index("(", at)
            for i in range(i, len(source)):
                depth += {"(": 1, ")": -1}.get(source[i], 0)
                if depth == 0:
                    break
            call = source[at:i + 1]
            assert "pr_base" in call, f"{name}: a PR opened without the resolved base:\n{call}"


def test_no_entry_point_writes_a_main_placeholder():
    """A missing branch stays None so the project's bound branch is not shadowed."""
    sources = {"main.py": API_MAIN.read_text()}
    for name in ("decisions.py", "metatron_captain.py", "plane_webhooks.py",
                 "orchestrator_next_move.py", "agent_process.py", "intent_router.py",
                 "prd_decomposition.py", "code_task_service.py"):
        sources[name] = (SERVICES / name).read_text()
    for name, source in sources.items():
        assert not re.search(r'base_branch=[^\n]*\bor "main"', source), name
        assert 'base_branch="main"' not in source, name
