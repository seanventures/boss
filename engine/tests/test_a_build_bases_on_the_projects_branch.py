"""A build clones, and opens its PR against, the PROJECT'S branch — not the GitHub default.

Project boss-platform binds paramg/boss with default_branch 'rel/rc10.10-kg' (the console's
New build form shows it). Its builds cloned `main` — the API wrote "main" when the form sent
no branch — and opened PRs #2/#3 against `main`, the GitHub default (crun_23d665a9,
crun_2f4eb5eb, 2026-10-26). One resolver (services/build_base_branch.py) now answers both, in
order: explicit spec base -> the project's binding -> the host default.

A default branch that MOVED (renamed master -> main, or switched) is followed, never failed on
or built from stale (BOSSAPP-1439): a recorded branch gone from the host, or one BOSS read from
the host that is no longer its default, gives way to the host's default; the binding is updated
and the build says so in words. A branch a person chose stays.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import build_base_branch as bbb
from boss_engine.services.software_factory import _accepts_kwarg

from .test_workflow_capability_runner import eng, project  # noqa: F401

SERVICES = Path(__file__).resolve().parents[1] / "boss_engine" / "services"
API_MAIN = Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "main.py"


@pytest.fixture()
def conn():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE github_repository_bindings (id TEXT, project_id TEXT, full_name TEXT,"
            " role TEXT DEFAULT 'primary', status TEXT DEFAULT 'active', default_branch TEXT,"
            " metadata TEXT DEFAULT '{}')"))
        yield c


def _bind(c, branch, *, project="boss-platform", full_name="paramg/boss", status="active",
          recorded_from=None):
    meta = '{"default_branch_from": "%s"}' % recorded_from if recorded_from else "{}"
    c.execute(text("INSERT INTO github_repository_bindings (id, project_id, full_name, role,"
                   " status, default_branch, metadata) VALUES (:i, :p, :f, 'primary', :s, :b, :m)"),
              {"i": f"b-{project}-{full_name}", "p": project, "f": full_name, "s": status,
               "b": branch, "m": meta})


def _resolve(c, explicit=None, *, host="main", project="boss-platform", branches=None):
    calls = []

    def host_default():
        calls.append(1)
        return host
    base = bbb.resolve_build_base(c, project_id=project, owner="paramg", repo="boss",
                                  explicit=explicit, host_default=host_default, run_id="crun_t",
                                  branch_exists=(branches.__contains__ if branches is not None
                                                 else None))
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


# --- a default branch that moved (BOSSAPP-1439) --------------------------------------------

def test_a_renamed_default_branch_is_followed_and_said(conn):
    """master was renamed main on GitHub: the recorded master is gone, so every build failed at
    the clone. Now it follows main, and says what moved and where to change it."""
    _bind(conn, "master")
    base, _ = _resolve(conn, host="main", branches={"main"})
    assert (base.clone, base.pr) == ("main", "main")
    assert base.clone_source == bbb.SOURCE_MOVED and base.moved_from == "master"
    assert "master no longer exists on paramg/boss" in base.note
    assert "its default is now main" in base.note and "Setup › Repositories" in base.note
    assert base.as_event()["note"] == base.note


def test_a_switched_default_the_host_gave_is_followed_not_built_stale(conn):
    """The default moved to main but master is still there: BOSS read master FROM the host, so
    building from it now is building from stale code."""
    _bind(conn, "master", recorded_from="host")
    base, _ = _resolve(conn, host="main", branches={"main", "master"})
    assert (base.clone, base.clone_source) == ("main", bbb.SOURCE_MOVED)
    assert "default branch is no longer master" in base.note


def test_a_branch_a_person_chose_is_kept_when_the_host_default_differs(conn):
    _bind(conn, "rel/x", recorded_from="person")
    base, _ = _resolve(conn, host="main", branches={"main", "rel/x"})
    assert (base.clone, base.clone_source, base.note) == ("rel/x", bbb.SOURCE_BINDING, None)
    conn.execute(text("DELETE FROM github_repository_bindings"))
    _bind(conn, "rel/x")                         # unmarked (bound before 1439): kept too
    assert _resolve(conn, host="main", branches={"main", "rel/x"})[0].clone == "rel/x"


def test_a_deleted_branch_a_person_chose_is_never_swapped_for_the_default(conn):
    _bind(conn, "release/2.0", recorded_from="person")
    base, _ = _resolve(conn, host="main", branches={"main"})
    assert (base.clone, base.clone_source, base.note) == ("release/2.0", bbb.SOURCE_BINDING, None)


def test_a_lookup_github_could_not_answer_is_not_a_deleted_branch(conn):
    _bind(conn, "master")
    base = bbb.resolve_build_base(conn, project_id="boss-platform", owner="paramg", repo="boss",
                                  host_default=lambda: "main",
                                  branch_exists=lambda b: None if b == "master" else True)
    assert (base.clone, base.clone_source) == ("master", bbb.SOURCE_BINDING)


def test_a_host_that_cannot_confirm_the_new_default_changes_nothing(conn):
    """branch_exists answers False on any error: "gone" alone never moves a build."""
    _bind(conn, "master")
    base, _ = _resolve(conn, host="main", branches=set())
    assert (base.clone, base.clone_source) == ("master", bbb.SOURCE_BINDING)


def test_a_spec_that_named_the_old_default_follows_the_move(conn):
    """A retry carries its first build's spec, which named master; master is gone."""
    _bind(conn, "master")
    base, _ = _resolve(conn, "master", host="main", branches={"main"})
    assert (base.clone, base.clone_source) == ("main", bbb.SOURCE_MOVED)
    # A spec that named some other branch, now gone, is the person's choice: as before.
    base, _ = _resolve(conn, "feature/gone", host="main", branches={"main"})
    assert (base.clone, base.clone_source) == ("feature/gone", bbb.SOURCE_SPEC)


def test_a_moved_default_updates_the_projects_binding(eng, project):  # noqa: F811
    from sqlalchemy import text as _t

    cid, bid = f"ghc_{project}", f"ghb_{project}"
    with eng.begin() as c:
        c.execute(_t("INSERT INTO github_connections (id, project_id, connection_label, "
                     "auth_method, credential_ref, created_by) VALUES (:i, :p, 'gh', 'pat', "
                     "'secret://gh', 'test')"), {"i": cid, "p": project})
        c.execute(_t("INSERT INTO github_repository_bindings (id, project_id, connection_id, "
                     "owner, name, full_name, default_branch, created_by) VALUES (:b, :p, :c, "
                     "'paramg', 'boss', 'paramg/boss', 'master', 'test')"),
                  {"b": bid, "p": project, "c": cid})
    with eng.begin() as c:
        base = bbb.resolve_build_base(c, project_id=project, owner="paramg", repo="boss",
                                      host_default=lambda: "main",
                                      branch_exists={"main"}.__contains__)
    assert base.clone == "main"
    with eng.begin() as c:
        row = c.execute(_t("SELECT default_branch, metadata FROM github_repository_bindings "
                           "WHERE id = :b"), {"b": bid}).mappings().one()
        moved = c.execute(_t("SELECT data FROM events WHERE event_type = "
                             "'github.default_branch_moved' AND entity_id = :b"),
                          {"b": bid}).scalar()
    assert row["default_branch"] == "main"
    assert row["metadata"]["default_branch_moved_from"] == "master"
    assert moved == {"from": "master", "to": "main"}
    # The next build reads main from the project, and asks nothing more of the host.
    with eng.begin() as c:
        again = bbb.resolve_build_base(c, project_id=project, owner="paramg", repo="boss",
                                       host_default=lambda: "main",
                                       branch_exists={"main"}.__contains__)
    assert (again.clone, again.clone_source, again.note) == ("main", bbb.SOURCE_BINDING, None)


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
        # Both can tell a moved default branch from a chosen one (BOSSAPP-1439).
        assert "branch_exists=" in source and "default_branch_moved" in source, name


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
