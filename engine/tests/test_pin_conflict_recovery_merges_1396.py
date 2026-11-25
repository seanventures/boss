"""BOSSAPP-1396: Pin Tier 1 and Tier 1b conflict-recovery merge retries to an exact head,
emit event on unreadable pin, and guide Re-check on LocalMergeMoved."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from boss_engine.config import Settings
from boss_engine.services import local_repo, merge_failures, software_factory
from boss_engine.services.github_adapter import GitHubAdapter, PullRequestMergeResult
from boss_engine.services.local_repo_host import LocalGitAdapter
from boss_engine.services.software_factory import SoftwareFactoryService


def test_commit_identity_exposes_parents_on_github_and_local_adapters(tmp_path, monkeypatch):
    # GitHubAdapter.commit_identity includes parents
    github = GitHubAdapter(
        Settings(BOSS_TARGET_GITHUB_OWNER="acme", BOSS_TARGET_GITHUB_REPO="w", BOSS_GITHUB_TOKEN="t")
    )
    monkeypatch.setattr(
        github,
        "_request",
        lambda m, p: {
            "sha": "c" * 40,
            "tree": {"sha": "t" * 40},
            "parents": [{"sha": "a" * 40}, {"sha": "b" * 40}],
        },
    )
    ident = github.commit_identity("c" * 40)
    assert ident["parents"] == ["a" * 40, "b" * 40]

    # LocalGitAdapter.commit_identity + restack include parents and head_sha
    git_dir = tmp_path / "repo.git"
    local_repo.init_bare(git_dir)
    sha1 = local_repo.commit_files(git_dir, "main", {"a.py": "v1\n"}, "c1")
    sha2 = local_repo.commit_files(git_dir, "main", {"a.py": "v2\n"}, "c2")
    local_parents = local_repo.commit_parents(git_dir, sha2)
    assert local_parents == [sha1]

    monkeypatch.setattr(local_repo, "root", lambda settings=None: tmp_path)
    adapter = LocalGitAdapter(Settings(), owner="local_x", repo="w")
    bare = adapter.git_dir
    bare.parent.mkdir(parents=True, exist_ok=True)
    local_repo.init_bare(bare)
    b_sha1 = local_repo.commit_files(bare, "main", {"a.py": "v1\n"}, "c1")
    b_sha2 = local_repo.commit_files(bare, "main", {"a.py": "v2\n"}, "c2")
    assert adapter.commit_identity(b_sha2)["parents"] == [b_sha1]


def test_local_merge_moved_guides_recheck_and_classifies_as_failed(tmp_path):
    git_dir = tmp_path / "repo.git"
    local_repo.init_bare(git_dir)
    local_repo.commit_files(git_dir, "main", {"README.md": "hi\n"}, "init")
    local_repo.ensure_branch(git_dir, "boss/code/1", "main")
    sha1 = local_repo.commit_files(git_dir, "boss/code/1", {"a.py": "v1\n"}, "c1")
    # Force-rewrite branch so sha1 is no longer an ancestor
    main_sha = local_repo.branch_sha(git_dir, "main")
    local_repo.git(["update-ref", "refs/heads/boss/code/1", main_sha], git_dir=git_dir)
    local_repo.commit_files(git_dir, "boss/code/1", {"a.py": "rewritten\n"}, "rewritten")
    with pytest.raises(local_repo.LocalMergeMoved) as caught:
        local_repo.merge(git_dir, "boss/code/1", "main", "Merge", at=sha1)
    msg = str(caught.value)
    assert "Re-check this change" in msg
    assert merge_failures.classify(msg) == merge_failures.FAILED


def test_tier1_pins_updated_head_and_refuses_when_parent_mismatches(monkeypatch):
    events: list[tuple] = []
    merge_calls: list[dict] = []
    approved = "1111111111111111111111111111111111111111"
    updated_good = "2222222222222222222222222222222222222222"
    updated_bad = "3333333333333333333333333333333333333333"
    monkeypatch.setattr(software_factory, "create_artifact", lambda *a, **k: "art_1")

    class _FakeGitHub:
        repository_full_name = "acme/widget"

        def __init__(self, new_head: str, parents: list[str]):
            self.new_head = new_head
            self.parents = parents

        def pull_request_state(self, _n):
            return {"state": "open", "merged": False}

        def mark_ready_for_review(self, _n):
            return True

        def pull_request_snapshot(self, _n):
            return {"head_sha": self.new_head, "state": "open", "merged": False}

        def commit_identity(self, sha: str):
            return {"commit_sha": sha, "parents": self.parents}

        def update_pull_request_branch(self, _n):
            return True

        def merge_pull_request(self, number, title, *, head_sha=None):
            merge_calls.append({"number": number, "title": title, "head_sha": head_sha})
            if head_sha == approved:
                raise RuntimeError("405 Method Not Allowed: not mergeable")
            return PullRequestMergeResult(sha="merged_sha", merged=True, message="ok")

    def _run_case(new_head: str, parents: list[str]):
        merge_calls.clear()
        events.clear()
        sf = object.__new__(SoftwareFactoryService)
        sf.settings = Settings()
        sf.github = _FakeGitHub(new_head, parents)
        sf.events = SimpleNamespace(emit=lambda *a, **k: events.append(a))
        sf.receipts = SimpleNamespace(
            create_or_reuse=lambda *a, **k: ({"id": "rcpt_1", "status": "pending"}, True),
            mark_failed=lambda *a, **k: None,
            mark_succeeded=lambda *a, **k: None,
        )
        sf.budget = SimpleNamespace(
            assert_active_reservation=lambda *a, **k: {"id": "res_1"},
            consume=lambda *a, **k: None,
        )
        sf.bind_project_github = lambda *a, **k: {}
        sf.github_plane = "direct"
        sf.github_binding_id = None
        sf.github_connection_id = None
        sf._approved_head_sha = staticmethod(lambda *a, **k: approved)
        sf._head_moved_past = lambda *_a, **_k: False
        sf._after_merge_failure = lambda _c, _p, _d, _n, _u, _b, _r, msg, **kw: {
            "status": "failed",
            "message": msg,
            **kw,
        }
        sf._mark_merge_completed = lambda *a, **k: None
        sf._scope = lambda *a, **k: {"org_id": "o", "workspace_id": "w", "project_id": "p"}
        def _exec(stmt, *_a, **_k):
            if "ci_changed" in str(stmt):
                return SimpleNamespace(
                    mappings=lambda: SimpleNamespace(first=lambda: None, all=lambda: []),
                    scalars=lambda: SimpleNamespace(all=lambda: []),
                )
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(
                    first=lambda: {
                        "status": "approved",
                        "task_id": "t1",
                        "data_boundary": "internal",
                        "approved_by": "person",
                        "recommendation": {
                            "action": "review_pull_request",
                            "pull_request_url": "https://github.com/acme/widget/pull/10",
                        },
                    },
                    all=lambda: [],
                ),
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )

        conn = SimpleNamespace(execute=_exec)
        return sf.merge_pull_request(
            conn, "p1", "t1", "dec_1", "dk_1", 10, "https://github.com/acme/widget/pull/10",
            "Title", "feature/1",
        )

    # Case 1: update_pull_request_branch creates merge commit with approved as parent -> pins to updated_good!
    ok_res = _run_case(updated_good, ["base_sha", approved])
    assert ok_res["merged_sha"] == "merged_sha"
    assert merge_calls == [
        {"number": 10, "title": "Title", "head_sha": approved},
        {"number": 10, "title": "Title", "head_sha": updated_good},
    ]

    # Case 2: head moved to an unrelated commit without approved in parents -> refuses second merge!
    bad_res = _run_case(updated_bad, ["other_sha"])
    assert bad_res["status"] == "failed"
    assert bad_res["pinned"] is True
    assert "moved after approval" in bad_res["message"]
    assert len(merge_calls) == 1


def test_tier1b_pins_to_restacked_head_and_refuses_when_unreadable():
    calls: list[dict] = []

    class _FakeGitHub:
        def last_merged_pull_request(self, _b):
            return {"number": 5, "head_sha": "aaa111"}

        def restack_after_merge(self, *_a, **_k):
            return {"restacked": True, "paths": ["x.py"], "base": "main", "head_sha": "restack_999"}

        def merge_pull_request(self, number, title, *, head_sha=None):
            calls.append({"number": number, "title": title, "head_sha": head_sha})
            return PullRequestMergeResult(sha="merged_sha", merged=True, message="ok")

    stub = SimpleNamespace(
        github=_FakeGitHub(),
        events=SimpleNamespace(emit=lambda *a, **k: None),
    )
    res = SoftwareFactoryService._merge_after_restack(
        stub, None, "p1", 6, "Title", "boss/code/1", "rcpt_1", "https://github.test/pr/6",
        approved_head="aaa111",
    )
    assert res is not None and res.merged is True
    assert calls == [{"number": 6, "title": "Title", "head_sha": "restack_999"}]

    # If restacked head_sha is missing and approved_head was set, refuse unpinned merge
    calls.clear()
    stub.github.restack_after_merge = lambda *_a, **_k: {"restacked": True, "paths": ["x.py"], "base": "main"}
    res_unreadable = SoftwareFactoryService._merge_after_restack(
        stub, None, "p1", 6, "Title", "boss/code/1", "rcpt_1", "https://github.test/pr/6",
        approved_head="aaa111",
    )
    assert res_unreadable is None
    assert calls == []


def test_unreadable_head_sha_emits_merge_head_pin_dropped_event(monkeypatch):
    events: list[tuple] = []
    monkeypatch.setattr(software_factory, "create_artifact", lambda *a, **k: "art_1")
    sf = object.__new__(SoftwareFactoryService)
    sf.settings = Settings()
    sf.github = SimpleNamespace(
        repository_full_name="acme/widget",
        pull_request_state=lambda _n: {"state": "open", "merged": False},
        mark_ready_for_review=lambda _n: True,
        merge_pull_request=lambda n, t: PullRequestMergeResult(sha="m", merged=True, message="ok"),
    )
    sf.events = SimpleNamespace(emit=lambda *a, **k: events.append(a))
    sf.receipts = SimpleNamespace(
        create_or_reuse=lambda *a, **k: ({"id": "rcpt_1", "status": "pending"}, True),
        mark_succeeded=lambda *a, **k: None,
    )
    sf.budget = SimpleNamespace(
        assert_active_reservation=lambda *a, **k: {"id": "res_1"},
        consume=lambda *a, **k: None,
    )
    sf.bind_project_github = lambda *a, **k: {}
    sf.github_plane = "direct"
    sf.github_binding_id = None
    sf.github_connection_id = None
    sf._mark_merge_completed = lambda *a, **k: None
    sf._scope = lambda *a, **k: {"org_id": "o", "workspace_id": "w", "project_id": "p"}

    class _Conn:
        def execute(self, stmt, *_a, **_k):
            if "recommendation->>'head_sha'" in str(stmt):
                raise RuntimeError("db json error")
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(
                    first=lambda: {
                        "status": "approved",
                        "task_id": "t1",
                        "data_boundary": "internal",
                        "approved_by": "person",
                        "recommendation": {
                            "action": "review_pull_request",
                            "pull_request_url": "https://github.com/acme/widget/pull/10",
                        },
                    },
                    all=lambda: [],
                ),
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )

    res = sf.merge_pull_request(
        _Conn(), "p1", "t1", "dec_1", "dk_1", 10, "https://github.com/acme/widget/pull/10",
        "Title", "feature/1",
    )
    assert res["merged_sha"] == "m"
    assert any(e[2] == "software_factory.merge_head_pin_dropped" for e in events)
