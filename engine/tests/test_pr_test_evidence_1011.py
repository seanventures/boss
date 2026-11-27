"""BOSSAPP-1011: Every PR carries test evidence — brief in the body, full results downloadable
and (on GitHub) published as a native Check Run on the head SHA.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from boss_engine.config import Settings
from boss_engine.schemas import FileChange
from boss_engine.services import github_actions, pr_description, test_evidence
from boss_engine.services.github_adapter import CheckRunResult, GitHubAdapter, PullRequestResult
from boss_engine.services.software_factory import SoftwareFactoryService


SAMPLE_PYTEST_OUTPUT = """
tests/test_words.py::test_empty PASSED
tests/test_words.py::test_whitespace PASSED
tests/test_words.py::test_unicode SKIPPED
tests/test_words.py::test_punctuation FAILED
========================= 1 failed, 2 passed, 1 skipped in 0.04s =========================
"""

SAMPLE_UNITTEST_OUTPUT = """
test_empty (tests.test_words.TestWords) ... ok
test_multi_word (tests.test_words.TestWords) ... ok

----------------------------------------------------------------------
Ran 2 tests in 0.001s

OK
"""


def test_parse_case_results_extracts_pytest_and_unittest_cases():
    py = test_evidence.parse_case_results(SAMPLE_PYTEST_OUTPUT)
    assert py["passed"] == 2
    assert py["failed"] == 1
    assert py["skipped"] == 1
    assert py["total"] == 4
    names = {c["name"]: c["status"] for c in py["cases"]}
    assert names["tests/test_words.py::test_empty"] == "passed"
    assert names["tests/test_words.py::test_punctuation"] == "failed"
    assert names["tests/test_words.py::test_unicode"] == "skipped"

    ut = test_evidence.parse_case_results(SAMPLE_UNITTEST_OUTPUT)
    assert ut["passed"] == 2 and ut["failed"] == 0 and ut["total"] == 2


def test_pr_description_always_ends_with_test_evidence_section():
    local_body = pr_description.render(
        title="Add word counter",
        evidence={
            "tests": [{"command": "python3 -m unittest -v", "ok": True, "exit_code": 0}],
            "tests_passed": True,
            "last_verify_output": SAMPLE_UNITTEST_OUTPUT,
            "substrate": "local",
            "verify_cwd": ".",
            "gates_skipped": {"security_check": "tier 0 build"},
        },
        gates={"written_tests_pass": True, "existing_tests_intact": True},
        paths=["textkit/words.py", "tests/test_words.py"],
        run_id="crun_1011",
        quality_gate_id="qg_1011",
        host_kind="local",
        head_sha="a" * 40,
        test_report_artifact_id="art_1011",
        check_run_url="https://github.com/org/repo/runs/999",  # ignored on local host!
    )
    assert test_evidence.has_test_evidence_section(local_body)
    assert test_evidence.ends_with_test_evidence_section(local_body)
    assert "### Test evidence" in local_body
    assert "2 cases (2 passed, 0 failed, 0 skipped)" in local_body
    assert "python3 -m unittest -v" in local_body
    assert "/v1/artifacts/art_1011/download" in local_body
    assert "security_check: tier 0 build" in local_body
    # Local host must NEVER mention GitHub or include a GitHub check-run URL.
    assert "github" not in local_body.lower()

    gh_body = pr_description.render(
        title="Add word counter",
        evidence={
            "tests": [{"command": "python3 -m unittest -v", "ok": True, "exit_code": 0}],
            "tests_passed": True,
            "last_verify_output": SAMPLE_UNITTEST_OUTPUT,
            "substrate": "local",
        },
        gates={"written_tests_pass": True},
        paths=["textkit/words.py"],
        host_kind="github",
        head_sha="b" * 40,
        test_report_artifact_id="art_1012",
        check_run_url="https://github.com/org/repo/runs/999",
    )
    assert test_evidence.ends_with_test_evidence_section(gh_body)
    assert "/v1/artifacts/art_1012/download" in gh_body
    assert "https://github.com/org/repo/runs/999" in gh_body


def test_github_actions_fix_pr_body_ends_with_test_evidence():
    body = github_actions.pull_request_body(
        title="fix: enforce https upstream",
        run_id="wfr_fix1",
        files=["tenants/acme.yaml"],
        report="SEC-004 passed after fix",
        summary="Updated upstream URL to https.",
        branch="boss/wfr_fix1-fix-acme",
        base="main",
    )
    assert test_evidence.has_test_evidence_section(body)
    assert test_evidence.ends_with_test_evidence_section(body)
    assert "**Result:**" in body
    assert "**Not run:**" in body


class _FakeReceiptService:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.by_id: dict[str, dict] = {}

    def create_or_reuse(self, _c, _org, _ws, _proj, dedupe_key, effect_kind, scope, payload, created_by_job_id=None):
        if dedupe_key in self.rows:
            return self.rows[dedupe_key], False
        rid = f"rcpt_{len(self.rows) + 1}"
        row = {"id": rid, "dedupe_key": dedupe_key, "effect_kind": effect_kind,
               "status": "pending", "result": {}, "payload": payload, "scope": scope}
        self.rows[dedupe_key] = row
        self.by_id[rid] = row
        return row, True

    def record_completed(self, _c, _org, _ws, _proj, dedupe_key, effect_kind, scope, payload,
                         provider_ref="", created_by_job_id=None, data_boundary="internal"):
        row, created = self.create_or_reuse(_c, _org, _ws, _proj, dedupe_key, effect_kind, scope, payload, created_by_job_id)
        row["status"] = "succeeded"
        row["provider_ref"] = provider_ref
        row["result"] = payload
        return row, created

    def mark_succeeded(self, _c, rid, provider_ref, result):
        self.by_id[rid]["status"] = "succeeded"
        self.by_id[rid]["provider_ref"] = provider_ref
        self.by_id[rid]["result"] = result

    def mark_failed(self, _c, rid, result):
        self.by_id[rid]["status"] = "failed"
        self.by_id[rid]["result"] = result


class _FakeGitHubHost:
    def __init__(self, *, kind: str = "github"):
        self.kind = kind
        self.owner = "acme"
        self.repo = "app"
        self.repository_full_name = "acme/app"
        self.supports_draft_pull_requests = (kind == "github")
        self.files: dict[str, str] = {}
        self.prs: list[dict] = []
        self.check_runs: list[dict] = []
        self.body_updates: list[tuple[int, str]] = []
        self.head_sha = "1" * 40

    def ensure_branch(self, branch, base=None):
        return self.head_sha

    def read_text_file(self, branch, path):
        return self.files.get(path)

    def upsert_file(self, branch, path, content, message):
        self.files[path] = content
        return self.head_sha

    def create_pull_request(self, branch, title, body, draft=None, base=None):
        num = len(self.prs) + 1
        url = (f"http://boss-local/local_p1/app/pull/{num}"
               if self.kind == "local" else f"https://github.com/acme/app/pull/{num}")
        self.prs.append({"number": num, "title": title, "body": body, "branch": branch, "url": url})
        return PullRequestResult(url=url, number=num)

    def update_pull_request_body(self, number, body):
        self.body_updates.append((number, body))
        for pr in self.prs:
            if pr["number"] == number:
                pr["body"] = body

    def create_check_run(self, **kwargs):
        cid = 500 + len(self.check_runs) + 1
        rec = {**kwargs, "id": cid, "html_url": f"https://github.com/acme/app/runs/{cid}"}
        self.check_runs.append(rec)
        return CheckRunResult(
            id=cid,
            html_url=rec["html_url"],
            status="completed",
            conclusion=kwargs.get("conclusion"),
            name=kwargs.get("name", "BOSS Test Evidence"),
        )


def _make_factory(monkeypatch, host: _FakeGitHubHost):
    settings = Settings(github_token="tok", target_github_owner="acme", target_github_repo="app")
    sf = SoftwareFactoryService(settings, owner="acme", repo="app")
    sf.github = host
    sf.receipts = _FakeReceiptService()
    sf.budget = SimpleNamespace(
        assert_active_reservation=lambda _c, _p, rid: {"id": rid, "amount_usd_micros": 1000},
        release=MagicMock(),
        settle=MagicMock(),
    )
    sf.events = SimpleNamespace(emit=MagicMock())
    sf.decisions = SimpleNamespace(create_blocking=MagicMock(return_value="dec_1"))
    sf.pipeline = SimpleNamespace(
        wait_for_verdict=lambda sha, **_: SimpleNamespace(
            to_pull_request_validation=lambda: SimpleNamespace(
                status="passed", summary="checks passed", head_sha=sha, completed=True, checks=[]
            )
        )
    )
    sf.sandbox_validation = SimpleNamespace(enabled=False)
    monkeypatch.setattr(sf, "bind_project_github", lambda *a, **k: {"github_plane": "primary"})
    monkeypatch.setattr(sf, "_block_github_effect_for_data_boundary", lambda *a, **k: None)
    monkeypatch.setattr(sf, "_resolve_agent_module", lambda *a, **k: {"id": "mod_1"})
    monkeypatch.setattr(sf, "_route_model_execution", lambda *a, **k: {
        "model_id": "m1", "source_id": "s1", "route_decision_log_id": "rl_1"
    })
    monkeypatch.setattr(sf, "_scope", lambda *a, **k: {"org_id": "org_1", "workspace_id": "ws_1"})
    monkeypatch.setattr(sf, "_review_packet_markdown", lambda *a, **k: "Packet notes.")
    monkeypatch.setattr(sf, "_data_boundary_for_task", lambda *a, **k: "internal")

    artifacts: dict[str, dict] = {}

    def _fake_create_artifact(_c, _s, _p, _t, atype, title, content, **_kw):
        aid = f"art_{atype}_{len(artifacts) + 1}"
        artifacts[aid] = {"id": aid, "type": atype, "title": title, "content": dict(content)}
        return aid

    monkeypatch.setattr("boss_engine.services.software_factory.create_artifact", _fake_create_artifact)
    monkeypatch.setattr("boss_engine.services.local_review.on_change_proposed", lambda *a, **k: {})
    return sf, artifacts


def test_enforcing_factory_pr_missing_test_evidence_is_refused(monkeypatch):
    """BOSSAPP-1011 enforcing test: a factory PR missing the Test evidence section is refused."""
    host = _FakeGitHubHost(kind="github")
    sf, _artifacts = _make_factory(monkeypatch, host)

    res = sf.propose_pull_request(
        object(), "proj_1", "task_1", reservation_id="res_1",
        dedupe_key="code:task:crun_missing", title="Fix bug",
        branch_name="boss/code/crun_missing",
        summary="Fixed the bug without any test evidence section.",
        files=[FileChange(path="app.py", content="x = 1\n")],
        require_test_evidence=True,
    )
    assert res["status"] == "refused"
    assert res["error"] == "missing_test_evidence"
    assert host.prs == [], "create_pull_request must never be called when Test evidence is missing"
    assert host.files == {}, "no commit should land when Test evidence is missing"
    sf.budget.release.assert_called_once()
    emitted = [c.args[2] for c in sf.events.emit.call_args_list]
    assert "software_factory.pull_request_refused_missing_test_evidence" in emitted

    # Also prove _open_pull_request itself refuses bodies missing trailing Test evidence.
    with pytest.raises(test_evidence.MissingTestEvidenceError):
        sf._open_pull_request("b", "t", "Body with no evidence")
    with pytest.raises(test_evidence.MissingTestEvidenceError):
        sf._open_pull_request(
            "b", "t",
            "### Test evidence\n- Result: passed\n\n### Trailing section after evidence\nOops",
        )


def test_github_host_publishes_check_run_and_links_it_while_local_host_is_download_only(monkeypatch):
    evidence = {
        "tests": [{"command": "python3 -m unittest -v", "ok": True, "exit_code": 0}],
        "tests_passed": True,
        "last_verify_output": SAMPLE_UNITTEST_OUTPUT,
        "substrate": "local",
    }
    pr_summary = pr_description.render(
        title="Word counter", evidence=evidence, gates={"written_tests_pass": True},
        paths=["words.py"], host_kind="github",
    )

    # 1) GitHub-bound project: publishes a Check Run on head_sha with full per-case output.
    gh_host = _FakeGitHubHost(kind="github")
    sf_gh, gh_artifacts = _make_factory(monkeypatch, gh_host)
    out_gh = sf_gh.propose_pull_request(
        object(), "proj_1", "task_1", reservation_id="res_1",
        dedupe_key="code:task:crun_gh", title="Word counter",
        branch_name="boss/code/crun_gh", summary=pr_summary,
        files=[FileChange(path="words.py", content="def f(): return 1\n")],
        evidence=evidence, require_test_evidence=True,
    )
    assert out_gh["status"] == "created"
    assert len(gh_host.check_runs) == 1
    cr = gh_host.check_runs[0]
    assert cr["head_sha"] == gh_host.head_sha
    assert cr["conclusion"] == "success"
    assert "test_empty (tests.test_words.TestWords)" in cr["text"]
    assert out_gh["check_run_url"] == cr["html_url"]
    # PR body ends with Test evidence and links both download and the check run.
    final_body = gh_host.prs[0]["body"]
    assert test_evidence.ends_with_test_evidence_section(final_body)
    assert f"/v1/artifacts/{out_gh['test_report_artifact_id']}/download" in final_body
    assert cr["html_url"] in final_body
    # Check run publication has a durable receipt.
    assert any(r["effect_kind"] == "github.check_run.create" and r["status"] == "succeeded"
               for r in sf_gh.receipts.by_id.values())

    # 2) Local-hosted project: console download ONLY — no check run published, no "GitHub" text.
    local_host = _FakeGitHubHost(kind="local")
    sf_loc, _loc_artifacts = _make_factory(monkeypatch, local_host)
    local_summary = pr_description.render(
        title="Word counter", evidence=evidence, gates={"written_tests_pass": True},
        paths=["words.py"], host_kind="local",
    )
    out_loc = sf_loc.propose_pull_request(
        object(), "proj_1", "task_1", reservation_id="res_2",
        dedupe_key="code:task:crun_loc", title="Word counter",
        branch_name="boss/code/crun_loc", summary=local_summary,
        files=[FileChange(path="words.py", content="def f(): return 1\n")],
        evidence=evidence, require_test_evidence=True,
    )
    assert out_loc["status"] == "created"
    assert local_host.check_runs == [], "local host must be console-download only"
    assert out_loc["check_run_url"] is None
    final_local_body = local_host.prs[0]["body"]
    assert test_evidence.ends_with_test_evidence_section(final_local_body)
    assert f"/v1/artifacts/{out_loc['test_report_artifact_id']}/download" in final_local_body
    assert "github" not in final_local_body.lower()


def test_github_adapter_create_check_run_posts_to_check_runs_endpoint(monkeypatch):
    captured = {}

    def fake_request(self, method, path, json_body=None, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json_body
        captured["kwargs"] = kwargs
        return {
            "id": 777,
            "html_url": "https://github.com/acme/app/runs/777",
            "status": "completed",
            "conclusion": "success",
            "name": json_body["name"],
        }

    monkeypatch.setattr(GitHubAdapter, "_request", fake_request)
    adapter = GitHubAdapter(
        Settings(), owner="acme", repo="app", token="ghp_test",
        api_base_url="https://api.github.com",
    )
    res = adapter.create_check_run(
        name="BOSS Test Evidence",
        head_sha="a" * 40,
        title="BOSS Test Evidence: Word counter",
        summary="### Test evidence\n- Result: Passed",
        text="Per-case output here",
        conclusion="success",
        external_id="art_123",
    )
    assert res == CheckRunResult(
        id=777,
        html_url="https://github.com/acme/app/runs/777",
        status="completed",
        conclusion="success",
        name="BOSS Test Evidence",
    )
    assert captured["method"] == "POST"
    assert captured["path"] == "/repos/acme/app/check-runs"
    assert captured["body"]["head_sha"] == "a" * 40
    assert captured["body"]["output"]["text"] == "Per-case output here"
    assert captured["body"]["external_id"] == "art_123"
    assert json.dumps(captured["body"])
