"""BOSSAPP-1395: GitHub follow-up branch isolation while parent merge card is pending,
stacked PR vs restack after parent merge, merge sha pin, and pinned 409 HEAD_MOVED."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from boss_engine.config import Settings
from boss_engine.multirepo import change_sets
from boss_engine.schemas import FileChange
from boss_engine.services import acceptance_gate, merge_failures, pr_follow_up, review_packet, software_factory
from boss_engine.services.github_adapter import GitHubAdapter, PullRequestValidationResult
from boss_engine.services.software_factory import SoftwareFactoryService


def _settings() -> Settings:
    return Settings(
        BOSS_TARGET_GITHUB_OWNER="acme",
        BOSS_TARGET_GITHUB_REPO="widget",
        BOSS_GITHUB_TOKEN="tok",
    )


def test_github_adapter_ensure_branch_uses_base_sha_and_merge_pins_sha(monkeypatch):
    github = GitHubAdapter(_settings(), api_base_url="https://api.github.com")
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method: str, path: str, json_body=None, **_kwargs):
        calls.append((method, path, json_body))
        if method == "GET" and path.endswith("/git/ref/heads/boss/code/fu_1"):
            req = httpx.Request("GET", f"https://api.github.com{path}")
            raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
        if method == "POST" and path.endswith("/git/refs"):
            return {"object": {"sha": json_body["sha"]}}
        if method == "PUT" and path.endswith("/pulls/12/merge"):
            return {"sha": "merged_sha_1", "merged": True, "message": "merged"}
        raise AssertionError(f"Unexpected: {method} {path}")

    monkeypatch.setattr(github, "_request", fake_request)
    sha = github.ensure_branch("boss/code/fu_1", "boss/code/root", base_sha="a" * 40)
    assert sha == "a" * 40
    assert calls[1] == (
        "POST",
        "/repos/acme/widget/git/refs",
        {"ref": "refs/heads/boss/code/fu_1", "sha": "a" * 40},
    )

    merged = github.merge_pull_request(12, "Merge #12", head_sha="b" * 40)
    assert merged.merged is True
    assert calls[2][2]["sha"] == "b" * 40


def test_github_adapter_pinned_409_head_modified_raises_clear_message(monkeypatch):
    github = GitHubAdapter(_settings(), api_base_url="https://api.github.com")

    def fake_request(method: str, path: str, json_body=None, **_kwargs):
        req = httpx.Request(method, f"https://api.github.com{path}")
        resp = httpx.Response(
            409,
            request=req,
            json={"message": "Head branch was modified. Review and try the merge again."},
        )
        raise httpx.HTTPStatusError("409 Conflict", request=req, response=resp)

    monkeypatch.setattr(github, "_request", fake_request)
    with pytest.raises(RuntimeError, match="Head branch was modified"):
        github.merge_pull_request(12, "Merge #12", head_sha="b" * 40)


def test_merge_failures_classify_distinguishes_generic_409_from_pinned_head_moved():
    assert merge_failures.classify("Client error '409 Conflict' for url .../merge") == (
        merge_failures.HEAD_CHANGED
    )
    assert merge_failures.classify(
        "Client error '409 Conflict' for url .../merge", pinned=True
    ) == merge_failures.HEAD_MOVED
    assert merge_failures.classify(
        "409 Conflict: Head branch was modified. Review and try the merge again."
    ) == merge_failures.HEAD_MOVED
    assert merge_failures.classify(
        "The change moved after approval; nothing was merged."
    ) == merge_failures.HEAD_MOVED


def test_pr_follow_up_prepare_branch_restacks_isolated_branch_after_parent_merged():
    restacked_calls: list[tuple] = []

    class _FakeGitHub:
        def open_pull_request_for(self, branch_name: str):
            return None  # both parent and followup have no open PR (parent merged)

        def last_merged_pull_request(self, branch_name: str):
            if branch_name == "boss/code/root":
                return {"number": 41, "url": "https://github.test/pr/41", "head_sha": "abc123"}
            return None

        def restack_after_merge(self, branch_name: str, merged_head_sha: str, **kwargs):
            restacked_calls.append((branch_name, merged_head_sha, kwargs))
            return {"restacked": True, "paths": ["a.py"], "base": "main", "head_sha": "def456"}

    out = pr_follow_up.prepare_branch(
        _FakeGitHub(),
        "boss/code/followup_1",
        base="main",
        paths=["a.py"],
        parent_branch="boss/code/root",
    )
    assert out is not None
    assert out["after"] == 41
    assert out["restacked"] is True
    assert restacked_calls == [
        ("boss/code/followup_1", "abc123", {"base": "main", "number": 41})
    ]


def test_pr_follow_up_prepare_branch_skips_when_parent_pr_still_open():
    class _FakeGitHub:
        def open_pull_request_for(self, branch_name: str):
            if branch_name == "boss/code/root":
                return {"number": 41, "url": "https://github.test/pr/41"}
            return None

        def last_merged_pull_request(self, _branch_name: str):
            raise AssertionError("should not check merged when parent PR is still open")

    out = pr_follow_up.prepare_branch(
        _FakeGitHub(),
        "boss/code/followup_1",
        base="main",
        paths=["a.py"],
        parent_branch="boss/code/root",
    )
    assert out is None


def _make_stub_software_factory(monkeypatch, github, *, decisions_obj=None, events_list=None):
    sf = object.__new__(SoftwareFactoryService)
    sf.settings = Settings(
        BOSS_TARGET_GITHUB_OWNER="acme",
        BOSS_TARGET_GITHUB_REPO="widget",
        BOSS_GITHUB_TOKEN="tok",
    )
    sf.github = github
    sf.github_plane = "github"
    sf.github_binding_id = None
    sf.github_connection_id = None
    sf.bind_project_github = lambda *_a, **_kw: {"github_plane": "github"}
    sf._block_github_effect_for_data_boundary = lambda *_a, **_kw: None
    sf._resolve_agent_module = lambda *_a, **_kw: None
    sf._route_model_execution = lambda *_a, **_kw: {
        "blocked": None,
        "model_id": "m1",
        "source_id": "src_1",
        "route_decision_log_id": "rdl_1",
    }
    sf._scope = lambda *_a, **_kw: {"org_id": "org_1", "workspace_id": "ws_1"}
    sf._review_packet_markdown = lambda *_a, **_kw: "review packet"
    sf._run_sandbox_validation = lambda *_a, **_kw: (None, None, None)
    sf._data_boundary_for_task = lambda *_a, **_kw: {"mode": "internal"}

    sf.budget = SimpleNamespace(
        assert_active_reservation=lambda _c, _p, res_id: {
            "id": res_id,
            "amount_usd_micros": 1000,
        },
        release=lambda *_a, **_kw: None,
        settle=lambda *_a, **_kw: None,
    )
    sf.receipts = SimpleNamespace(
        create_or_reuse=lambda *_a, **_kw: (
            {"id": "rcpt_1", "status": "pending", "result": None},
            True,
        ),
        mark_succeeded=lambda *_a, **_kw: None,
        mark_failed=lambda *_a, **_kw: None,
    )
    events = events_list if events_list is not None else []
    sf.events = SimpleNamespace(
        emit=lambda _c, _p, event_type, _actor, _etype, _eid, payload: events.append(
            (event_type, payload)
        )
    )
    sf.pipeline = SimpleNamespace(
        wait_for_verdict=lambda sha, **_kw: SimpleNamespace(
            to_pull_request_validation=lambda: PullRequestValidationResult(
                status="passed",
                summary="passed",
                head_sha=sha,
                completed=True,
                checks=[],
            )
        )
    )
    if decisions_obj is None:
        decision_calls: list[dict] = []

        class _Decisions:
            def create_blocking(self, _c, _p, _t, title, rec, _ctx, kind):
                decision_calls.append({"title": title, "rec": rec, "kind": kind})
                return "dec_new_1"

        decisions_obj = _Decisions()
        decisions_obj.calls = decision_calls
    sf.decisions = decisions_obj

    art_counter = {"n": 0}

    def _fake_create_artifact(*_a, **_kw):
        art_counter["n"] += 1
        return f"art_{art_counter['n']}"

    monkeypatch.setattr(software_factory, "create_artifact", _fake_create_artifact)
    monkeypatch.setattr(change_sets, "member_change_set", lambda *_a, **_kw: None)
    monkeypatch.setattr(change_sets, "record_single", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        acceptance_gate,
        "merge_card_checks",
        lambda *_a, **_kw: (None, {"verdict": "met", "counts": {"met": 1}, "items": []}),
    )
    monkeypatch.setattr(review_packet, "stamp_evidence_head", lambda *_a, **_kw: None)
    monkeypatch.setattr(review_packet, "for_run", lambda *_a, **_kw: None)
    return sf, events


def test_software_factory_propose_uses_parent_branch_as_stacked_pr_base(monkeypatch):
    created_prs: list[tuple[str, str, str | None]] = []
    ensured: list[tuple[str, str | None]] = []

    class _GitHub:
        repository_full_name = "acme/widget"
        kind = "github"
        supports_draft_pull_requests = False

        def ensure_branch(self, branch_name: str, base_branch: str | None = None):
            ensured.append((branch_name, base_branch))
            return "sha_fu"

        def open_pull_request_for(self, branch_name: str):
            if branch_name == "boss/code/root":
                return {"number": 41, "url": "https://github.test/pr/41"}
            return None

        def last_merged_pull_request(self, _branch_name: str):
            raise AssertionError("should not restack while parent PR is still open")

        def upsert_file(self, _branch: str, _path: str, _content: str, _msg: str):
            return "sha_commit_1"

        def create_pull_request(self, branch_name, title, body, *, base=None, **_kwargs):
            created_prs.append((branch_name, title, base))
            return SimpleNamespace(number=42, url="https://github.test/pr/42", head_sha="sha_commit_1")

    sf, _events = _make_stub_software_factory(monkeypatch, _GitHub())
    out = sf.propose_pull_request(
        object(),
        "proj_1",
        "task_1",
        "res_1",
        "dedupe_1",
        "Follow-up fix",
        "boss/code/fu_1",
        "Fix item",
        [FileChange(path="a.py", content="x = 1\n")],
        pr_base="main",
        parent_branch="boss/code/root",
    )
    assert out["status"] == "created"
    assert ensured == [("boss/code/fu_1", "boss/code/root")]
    assert created_prs == [("boss/code/fu_1", "Follow-up fix", "boss/code/root")]
    assert sf.decisions.calls[0]["rec"]["pr_base"] == "boss/code/root"


def test_software_factory_propose_restacks_when_parent_pr_merged(monkeypatch):
    created_prs: list[tuple[str, str, str | None]] = []
    restack_calls: list[tuple[str, str, dict]] = []

    class _GitHub:
        repository_full_name = "acme/widget"
        kind = "github"
        supports_draft_pull_requests = False

        def ensure_branch(self, branch_name: str, base_branch: str | None = None):
            return "sha_fu"

        def open_pull_request_for(self, _branch_name: str):
            return None

        def last_merged_pull_request(self, branch_name: str):
            if branch_name == "boss/code/root":
                return {"number": 41, "url": "https://github.test/pr/41", "head_sha": "sha_root"}
            return None

        def restack_after_merge(self, branch_name: str, merged_head_sha: str, **kwargs):
            restack_calls.append((branch_name, merged_head_sha, kwargs))
            return {"restacked": True, "paths": ["a.py"], "base": "main", "head_sha": "sha_restacked"}

        def upsert_file(self, _branch: str, _path: str, _content: str, _msg: str):
            return "sha_commit_2"

        def create_pull_request(self, branch_name, title, body, *, base=None, **_kwargs):
            created_prs.append((branch_name, title, base))
            return SimpleNamespace(number=43, url="https://github.test/pr/43", head_sha="sha_commit_2")

    sf, events = _make_stub_software_factory(monkeypatch, _GitHub())
    out = sf.propose_pull_request(
        object(),
        "proj_1",
        "task_1",
        "res_1",
        "dedupe_2",
        "Follow-up fix",
        "boss/code/fu_1",
        "Fix item",
        [FileChange(path="a.py", content="x = 2\n")],
        pr_base="main",
        parent_branch="boss/code/root",
    )
    assert out["status"] == "created"
    assert restack_calls == [("boss/code/fu_1", "sha_root", {"base": "main", "number": 41})]
    assert created_prs == [("boss/code/fu_1", pr_follow_up.follow_up_title("Follow-up fix", 41), "main")]
    assert any(ev == "software_factory.follow_up_after_merge" for ev, _ in events)
    assert sf.decisions.calls[0]["rec"]["pr_base"] == "main"


def test_propose_pull_request_restores_superseded_pending_cards_if_create_blocking_raises(monkeypatch):
    card_statuses = {"dec_old_pending": "pending"}
    pr_url = "https://github.test/acme/widget/pull/42"

    class _NestedTx:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

    class _MappingsResult:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return self

        def all(self):
            return self._rows

    class _FakeConnection:
        def begin_nested(self):
            return _NestedTx()

        def execute(self, stmt, params=None):
            text_sql = str(stmt)
            if "SELECT id, recommendation->>'pull_request_url' AS u" in text_sql:
                rows = [
                    {"id": dec_id, "u": pr_url}
                    for dec_id, status in card_statuses.items()
                    if status == "pending"
                ]
                return _MappingsResult(rows)
            if "SET status = 'withdrawn'" in text_sql:
                for dec_id in (params or {}).get("ids") or []:
                    if card_statuses.get(dec_id) == "pending":
                        card_statuses[dec_id] = "withdrawn"
                return _MappingsResult([])
            if "SET status = 'pending'" in text_sql:
                for dec_id in (params or {}).get("ids") or []:
                    if card_statuses.get(dec_id) == "withdrawn":
                        card_statuses[dec_id] = "pending"
                return _MappingsResult([])
            return _MappingsResult([])

    class _FailingDecisions:
        def create_blocking(self, *_a, **_kw):
            assert card_statuses["dec_old_pending"] == "withdrawn"
            raise RuntimeError("transient decision insert failure")

    class _GitHub:
        repository_full_name = "acme/widget"
        kind = "github"
        supports_draft_pull_requests = False

        def ensure_branch(self, _branch_name: str, _base_branch: str | None = None):
            return "sha_fu"

        def open_pull_request_for(self, _branch_name: str):
            return None

        def last_merged_pull_request(self, _branch_name: str):
            return None

        def upsert_file(self, _branch: str, _path: str, _content: str, _msg: str):
            return "sha_commit_1"

        def create_pull_request(self, _branch_name, _title, _body, **_kwargs):
            return SimpleNamespace(number=42, url=pr_url, head_sha="sha_commit_1")

    sf, _events = _make_stub_software_factory(
        monkeypatch, _GitHub(), decisions_obj=_FailingDecisions()
    )
    with pytest.raises(RuntimeError, match="transient decision insert failure"):
        sf.propose_pull_request(
            _FakeConnection(),
            "proj_1",
            "task_1",
            "res_1",
            "dedupe_3",
            "Re-check change",
            "boss/code/fu_1",
            "Re-check",
            [FileChange(path="a.py", content="x = 3\n")],
            run_id="crun_fu_new",
        )
    assert card_statuses["dec_old_pending"] == "pending"
