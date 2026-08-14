"""Unit tests for scripts/qa/factory_e2e_gate.py (no network)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "qa" / "factory_e2e_gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("factory_e2e_gate", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module()


def test_join_url_is_2xx_extract_run_id():
    assert gate.join_url("https://x/", "v1/code-tasks") == "https://x/v1/code-tasks"
    assert gate.is_2xx(200)
    assert gate.is_2xx(201)
    assert not gate.is_2xx(422)
    assert gate.extract_run_id({"run_id": "r1", "status": "queued"}) == "r1"
    assert gate.extract_run_id({"id": "r2"}) == "r2"
    assert gate.extract_run_id({}) is None


def test_code_task_create_body_shape():
    body = gate.code_task_create_body(
        "boss-dogfood",
        owner="acme",
        repo="app",
        base_branch="develop",
        run_tag="t1",
    )
    assert body["project_id"] == "boss-dogfood"
    assert body["owner"] == "acme"
    assert body["repo"] == "app"
    assert body["base_branch"] == "develop"
    assert body["open_pr"] is False
    assert gate.COMMAND_PREFIX in body["title"]
    assert gate.COMMAND_PREFIX in body["instructions"]


def test_code_task_fixture_body_is_simulated_no_pr():
    body = gate.code_task_fixture_body("boss-dogfood", run_tag="fx")
    assert body["simulated"] is True
    assert body["open_pr"] is False
    assert body["owner"] == "_fixture"
    assert body["repo"] == "_fixture"
    assert "simulated" in body["instructions"]
    assert gate.COMMAND_PREFIX in body["title"]


def test_structural_endpoints_catalog():
    names = {e["name"] for e in gate.STRUCTURAL_ENDPOINTS}
    assert "health" in names
    assert "factory.wip" in names
    assert "code_tasks.list" in names
    assert "code_tasks.create" in names
    assert "code_tasks.create_fixture" in names
    assert "factory.live_pr" in names


def test_classify_proposals_queue_405_is_skip_not_fail():
    assert gate.classify_proposals_queue(405) == "skip"
    assert gate.classify_proposals_queue(404) == "skip"
    assert gate.classify_proposals_queue(200) == "pass"
    assert gate.classify_proposals_queue(201) == "pass"
    assert gate.classify_proposals_queue(500) == "fail"
    assert gate.classify_proposals_queue(403) == "fail"
    assert gate.classify_proposals_queue(None) == "fail"


def test_fixture_create_allowed_requires_advertised_flag():
    assert gate.fixture_create_allowed({"fixture_supported": True}) is True
    assert gate.fixture_create_allowed({"fixture_supported": False}) is False
    assert gate.fixture_create_allowed({}) is False
    assert gate.fixture_create_allowed(None) is False
    assert gate.fixture_create_allowed([]) is False


def test_dry_run_structure_passes():
    checks = gate.validate_dry_run_structure(
        api_base="https://boss-api.example",
        project_id="boss-dogfood",
    )
    required = [c for c in checks if c.required]
    assert required
    assert all(c.ok for c in required)
    names = {c.name for c in checks}
    assert "structure.quality_loop_module" in names
    assert "structure.scorecard_v11_ui_ux" in names
    assert "structure.factory_endpoints_remain" in names
    assert "structure.quality_loop_contract" in names
    assert "structure.no_budgeted_code_task" in names
    assert "structure.agent_process_module" in names
    assert "structure.process_compiler_module" in names
    assert "structure.process_manager_module" in names
    assert "structure.scorecard_honesty" in names
    # env docs check present as optional
    docs = next(c for c in checks if c.name == "structure.live_env_docs")
    assert docs.ok
    assert "BOSS_API_KEY" in docs.detail


def test_dry_run_main_exit_0(monkeypatch, tmp_path):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    out = tmp_path / "factory-report.json"
    code = gate.main(["--dry-run", "--output", str(out), "--quiet"])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["gate"] == gate.GATE_NAME
    assert report["mode"] == "dry-run"
    assert report["summary"]["passed"] is True
    assert "env_for_full_live" in report
    assert any("BOSS_API_KEY" in x for x in report["env_for_full_live"]["required"])


def test_implicit_dry_run_without_key(monkeypatch, tmp_path, capsys):
    """Without API key, live invocation falls back to structural dry-run."""
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_BOSS_API_KEY", raising=False)
    out = tmp_path / "implicit.json"
    code = gate.main(["--output", str(out), "--quiet"])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["mode"] == "dry-run"
    err = capsys.readouterr().err
    assert "no BOSS_API_KEY" in err or report["summary"]["passed"]


def test_create_without_key_returns_2(monkeypatch, capsys):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_BOSS_API_KEY", raising=False)
    code = gate.main(["--create-code-task"])
    assert code == 2
    assert "BOSS_API_KEY" in capsys.readouterr().err


def test_create_fixture_without_key_returns_2(monkeypatch, capsys):
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_BOSS_API_KEY", raising=False)
    code = gate.main(["--create-fixture"])
    assert code == 2
    assert "BOSS_API_KEY" in capsys.readouterr().err


def test_create_fixture_and_create_code_task_mutually_exclusive(monkeypatch, capsys):
    monkeypatch.setenv("BOSS_API_KEY", "test-key")
    code = gate.main(["--create-fixture", "--create-code-task"])
    assert code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_allow_create_from_env(monkeypatch):
    monkeypatch.delenv("BOSS_FACTORY_ALLOW_CREATE", raising=False)
    assert gate.allow_create_from_env() is False
    monkeypatch.setenv("BOSS_FACTORY_ALLOW_CREATE", "1")
    assert gate.allow_create_from_env() is True


def test_resolve_factory_repo(monkeypatch):
    monkeypatch.setenv("BOSS_FACTORY_OWNER", "o1")
    monkeypatch.setenv("BOSS_FACTORY_REPO", "r1")
    monkeypatch.setenv("BOSS_FACTORY_BASE_BRANCH", "dev")
    owner, repo, base = gate.resolve_factory_repo()
    assert (owner, repo, base) == ("o1", "r1", "dev")


def test_gate_exit_code():
    fail = gate.build_report(
        api_url="u",
        project_id="p",
        checks=[gate.CheckResult(name="x", ok=False, required=True)],
        started_at="a",
        finished_at="b",
        duration_ms=1,
        mode="live",
    )
    assert gate.gate_exit_code(fail) == 1
    ok = gate.build_report(
        api_url="u",
        project_id="p",
        checks=[gate.CheckResult(name="x", ok=True, required=True)],
        started_at="a",
        finished_at="b",
        duration_ms=1,
        mode="dry-run",
    )
    assert gate.gate_exit_code(ok) == 0


def test_proposals_queue_405_is_skip_not_optional_fail():
    """GET 405 must print SKIP and must not land in failed_optional_names."""
    class _Client:
        def request(self, method, path, **_kwargs):
            if path.startswith("/health"):
                return 200, {"status": "ok"}, 1
            if "/factory/wip" in path:
                return 200, {"items": [], "fixture_supported": True}, 1
            if path.startswith("/v1/code-tasks?") or path.startswith("/v1/code-tasks"):
                return 200, {"code_tasks": []}, 1
            if "proposals/queue" in path:
                return 405, {"detail": "Method Not Allowed"}, 1
            return 500, {}, 1

    checks = gate.run_gate(_Client(), project_id="boss-dogfood")
    queue = next(c for c in checks if c.name == "factory.proposals_queue")
    assert queue.skipped is True
    assert queue.ok is True
    assert queue.required is False
    assert queue.status_code == 405
    assert gate.check_flag(queue) == "SKIP"
    report = gate.build_report(
        api_url="u",
        project_id="p",
        checks=checks,
        started_at="a",
        finished_at="b",
        duration_ms=1,
        mode="live+readonly",
    )
    assert report["summary"]["passed"] is True
    assert "factory.proposals_queue" not in report["summary"]["failed_optional_names"]
    assert "factory.proposals_queue" in report["summary"]["skipped_names"]
    assert report["summary"]["optional_failed"] == 0


def test_proposals_queue_500_is_optional_fail():
    class _Client:
        def request(self, method, path, **_kwargs):
            if path.startswith("/health"):
                return 200, {"status": "ok"}, 1
            if "/factory/wip" in path:
                return 200, {"items": []}, 1
            if path.startswith("/v1/code-tasks"):
                return 200, {"code_tasks": []}, 1
            if "proposals/queue" in path:
                return 500, {"detail": "boom"}, 1
            return 500, {}, 1

    checks = gate.run_gate(_Client(), project_id="boss-dogfood")
    queue = next(c for c in checks if c.name == "factory.proposals_queue")
    assert queue.skipped is False
    assert queue.ok is False
    assert gate.check_flag(queue) == "FAIL"
    report = gate.build_report(
        api_url="u",
        project_id="p",
        checks=checks,
        started_at="a",
        finished_at="b",
        duration_ms=1,
        mode="live+readonly",
    )
    # Optional fail must not fail the gate, but is recorded (not a skip).
    assert report["summary"]["passed"] is True
    assert "factory.proposals_queue" in report["summary"]["failed_optional_names"]


def test_create_fixture_skips_when_api_does_not_advertise_support():
    posts: list[tuple[str, object]] = []

    class _Client:
        def request(self, method, path, **kwargs):
            if method == "POST":
                posts.append((path, kwargs.get("body")))
            if path.startswith("/health"):
                return 200, {"status": "ok"}, 1
            if "/factory/wip" in path:
                return 200, {"items": []}, 1  # no fixture_supported
            if path.startswith("/v1/code-tasks"):
                return 200, {"code_tasks": []}, 1
            if "proposals/queue" in path:
                return 405, {}, 1
            return 500, {}, 1

    checks = gate.run_gate(_Client(), project_id="boss-dogfood", create_fixture=True)
    fixture = next(c for c in checks if c.name == "code_tasks.create_fixture")
    assert fixture.skipped is True
    assert fixture.ok is True
    assert gate.check_flag(fixture) == "SKIP"
    assert posts == []
    assert not any(c.name == "code_tasks.fixture_evidence" for c in checks)


def test_create_fixture_posts_and_reads_evidence_when_supported():
    posts: list[dict] = []

    class _Client:
        def request(self, method, path, **kwargs):
            if path.startswith("/health"):
                return 200, {"status": "ok"}, 1
            if "/factory/wip" in path:
                return 200, {"items": [], "fixture_supported": True}, 1
            if method == "POST" and path == "/v1/code-tasks":
                body = kwargs.get("body") or {}
                posts.append(body)
                assert body.get("simulated") is True
                return 200, {"status": "simulated", "run_id": "crun_fix", "simulated": True}, 1
            if path == "/v1/code-tasks/crun_fix":
                return 200, {
                    "id": "crun_fix",
                    "status": "simulated",
                    "phase": "fixture",
                    "spec": {"simulated": True},
                    "usage": {"cost_usd_micros": 0, "simulated": True},
                }, 1
            if path.startswith("/v1/code-tasks"):
                return 200, {"code_tasks": []}, 1
            if "proposals/queue" in path:
                return 405, {}, 1
            return 500, {}, 1

    checks = gate.run_gate(_Client(), project_id="boss-dogfood", create_fixture=True)
    created = next(c for c in checks if c.name == "code_tasks.create_fixture")
    evidence = next(c for c in checks if c.name == "code_tasks.fixture_evidence")
    assert created.ok is True and created.skipped is False
    assert created.required is True
    assert evidence.ok is True
    assert posts and posts[0]["simulated"] is True
    report = gate.build_report(
        api_url="u",
        project_id="p",
        checks=checks,
        started_at="a",
        finished_at="b",
        duration_ms=1,
        mode="live+fixture",
    )
    assert report["summary"]["passed"] is True
    assert report["summary"]["required_failed"] == 0


def test_check_flag_skip_beats_fail():
    skip = gate.CheckResult(name="x", ok=True, skipped=True)
    assert gate.check_flag(skip) == "SKIP"
    fail = gate.CheckResult(name="y", ok=False)
    assert gate.check_flag(fail) == "FAIL"


def _readonly_client(**overrides):
    class _Client:
        def request(self, method, path, **kwargs):
            if method == "POST":
                raise AssertionError(f"no-spend path must not POST {path}")
            if path.startswith("/health"):
                return 200, {"status": "ok"}, 1
            if "/factory/wip" in path:
                payload = {
                    "items": [],
                    "fixture_supported": True,
                    "github_plane": "mission_control_binding",
                    "last_simulated_fixture": None,
                    "live_pr_success": {
                        "rate": None,
                        "label": "unknown / not proven",
                        "proven": False,
                        "sample_size": 0,
                    },
                }
                payload.update(overrides.get("wip", {}))
                return 200, payload, 1
            if path.startswith("/v1/code-tasks"):
                return 200, {"code_tasks": []}, 1
            if "proposals/queue" in path:
                return 405, {}, 1
            return 500, {}, 1

    return _Client()


def test_honesty_from_wip_rejects_invented_percent():
    empty = gate.honesty_from_wip({})
    assert empty["live_pr_success"]["label"] == gate.LIVE_PR_UNKNOWN_LABEL
    assert empty["live_pr_success"]["rate"] is None
    fake = gate.honesty_from_wip({"live_pr_success": {"label": "87%", "rate": 0.87}})
    assert fake["live_pr_success"]["label"] == gate.LIVE_PR_UNKNOWN_LABEL


def test_live_pr_without_canaries_fails_closed_and_does_not_post(monkeypatch):
    monkeypatch.delenv("BOSS_QA_ALLOW_CANARIES", raising=False)
    posts: list[object] = []

    class _Client:
        def request(self, method, path, **kwargs):
            if method == "POST":
                posts.append((path, kwargs.get("body")))
            if path.startswith("/health"):
                return 200, {"status": "ok"}, 1
            if "/factory/wip" in path:
                return 200, {"items": [], "fixture_supported": True}, 1
            if path.startswith("/v1/code-tasks"):
                return 200, {"code_tasks": []}, 1
            if "proposals/queue" in path:
                return 405, {}, 1
            return 500, {}, 1

    checks = gate.run_gate(_Client(), project_id="boss-dogfood", live_pr=True)
    live = next(c for c in checks if c.name == "factory.live_pr")
    assert live.required is False
    assert live.skipped is False
    assert live.ok is False
    assert "FAIL CLOSED" in live.detail
    assert "unknown / not proven" in live.detail
    assert posts == []
    report = gate.build_report(
        api_url="u",
        project_id="p",
        checks=checks,
        started_at="a",
        finished_at="b",
        duration_ms=1,
        mode="live+readonly",
    )
    assert report["summary"]["passed"] is True
    assert "factory.live_pr" in report["summary"]["failed_optional_names"]


def test_live_pr_with_canaries_still_does_not_post(monkeypatch):
    monkeypatch.setenv("BOSS_QA_ALLOW_CANARIES", "1")
    posts: list[object] = []

    class _Client:
        def request(self, method, path, **kwargs):
            if method == "POST":
                posts.append((path, kwargs.get("body")))
            if path.startswith("/health"):
                return 200, {"status": "ok"}, 1
            if "/factory/wip" in path:
                return 200, {"items": []}, 1
            if path.startswith("/v1/code-tasks"):
                return 200, {"code_tasks": []}, 1
            if "proposals/queue" in path:
                return 405, {}, 1
            return 500, {}, 1

    checks = gate.run_gate(_Client(), project_id="boss-dogfood", live_pr=True)
    live = next(c for c in checks if c.name == "factory.live_pr")
    assert live.ok is False
    assert live.required is False
    assert posts == []


def test_sealed_slice_honesty_is_optional_and_records_unknown(monkeypatch):
    monkeypatch.delenv("BOSS_QA_ALLOW_CANARIES", raising=False)
    checks = gate.run_gate(_readonly_client(), project_id="boss-dogfood")
    honesty = next(c for c in checks if c.name == "factory.sealed_slice_honesty")
    assert honesty.required is False
    assert honesty.ok is True
    assert honesty.evidence["live_pr_success"]["label"] == "unknown / not proven"
    live = next(c for c in checks if c.name == "factory.live_pr")
    assert live.skipped is True
    assert live.ok is True


def test_allow_live_pr_from_env(monkeypatch):
    monkeypatch.delenv("BOSS_QA_ALLOW_CANARIES", raising=False)
    assert gate.allow_live_pr_from_env() is False
    monkeypatch.setenv("BOSS_QA_ALLOW_CANARIES", "1")
    assert gate.allow_live_pr_from_env() is True


# ---------------------------------------------------------------------------
# Factory quality surface (structure + live read-only)
# ---------------------------------------------------------------------------

QUALITY_EXPORTS = (
    "should_run_quality_pass",
    "collect_workspace_evidence",
    "quality_improve_prompt",
)


def test_quality_loop_module_exists_and_exports():
    inspection = gate.inspect_quality_loop()
    assert inspection["exists"] is True
    assert inspection["path"].endswith("engine/boss_engine/services/factory_quality_loop.py")
    for name in QUALITY_EXPORTS:
        assert inspection["exports"][name] is True, name


def test_scorecard_v11_includes_ui_and_ux_dimensions():
    card = gate.inspect_scorecard_v11()
    assert card["version"] == "1.1"
    assert "ui" in card["dimension_ids"]
    assert "ux" in card["dimension_ids"]


def test_factory_endpoints_remain_in_catalog():
    paths = gate.factory_catalog_paths()
    assert "/v1/factory/wip" in paths
    assert "/v1/code-tasks" in paths


def test_quality_loop_contract_max_one_pass_never_invent_no_edit_tests():
    contract = gate.inspect_quality_loop_contract()
    assert contract["max_quality_passes"] == 1
    assert contract["never_invent_scores"] is True
    assert contract["do_not_edit_frozen_tests"] is True


def test_inspect_falls_back_to_file_text_when_import_fails(monkeypatch):
    """Dry-run must still prove the contract if engine is not on PYTHONPATH."""
    monkeypatch.setattr(gate, "_try_import_engine", lambda _name: None)
    inspection = gate.inspect_quality_loop()
    assert inspection["imported"] is False
    assert inspection["exists"] is True
    for name in QUALITY_EXPORTS:
        assert inspection["exports"][name] is True, name
    card = gate.inspect_scorecard_v11()
    assert card["imported"] is False
    assert card["version"] == "1.1"
    assert card["has_ui"] is True
    assert card["has_ux"] is True
    contract = gate.inspect_quality_loop_contract()
    assert contract["max_quality_passes"] == 1
    assert contract["never_invent_scores"] is True
    assert contract["do_not_edit_frozen_tests"] is True


def test_dry_run_structure_includes_quality_surface_checks():
    checks = gate.validate_dry_run_structure(
        api_base="https://boss-api.example",
        project_id="boss-dogfood",
    )
    by_name = {c.name: c for c in checks}
    loop = by_name["structure.quality_loop_module"]
    assert loop.ok is True
    assert loop.required is True
    for name in QUALITY_EXPORTS:
        assert loop.evidence["exports"][name] is True
    card = by_name["structure.scorecard_v11_ui_ux"]
    assert card.ok is True
    assert card.evidence["version"] == "1.1"
    assert "ui" in card.evidence["dimension_ids"]
    assert "ux" in card.evidence["dimension_ids"]
    endpoints = by_name["structure.factory_endpoints_remain"]
    assert endpoints.ok is True
    assert "/v1/factory/wip" in endpoints.evidence["paths"]
    assert "/v1/code-tasks" in endpoints.evidence["paths"]
    contract = by_name["structure.quality_loop_contract"]
    assert contract.ok is True
    assert contract.evidence["max_quality_passes"] == 1
    assert contract.evidence["never_invent_scores"] is True
    assert contract.evidence["do_not_edit_frozen_tests"] is True
    no_post = by_name["structure.no_budgeted_code_task"]
    assert no_post.ok is True
    assert no_post.evidence["create_required"] is False
    process = by_name["structure.agent_process_module"]
    assert process.ok is True
    honesty = by_name["structure.scorecard_honesty"]
    assert honesty.ok is True
    assert honesty.evidence["invents_a11y_violations"] is False
    assert honesty.evidence["invents_contrast_ok"] is False
    assert honesty.evidence["unknown_jtbd_not_scored"] is True


def test_dry_run_never_posts_budgeted_code_task(monkeypatch, tmp_path):
    """--dry-run --create-code-task must stay offline and must not POST."""
    monkeypatch.delenv("BOSS_API_KEY", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_BOSS_API_KEY", raising=False)
    monkeypatch.delenv("BOSS_FACTORY_ALLOW_CREATE", raising=False)

    def boom(*_args, **_kwargs):
        raise AssertionError("dry-run must not open the network")

    monkeypatch.setattr(gate, "urlopen", boom)
    out = tmp_path / "factory-no-post.json"
    code = gate.main(
        ["--dry-run", "--create-code-task", "--output", str(out), "--quiet"]
    )
    assert code == 0
    report = json.loads(out.read_text())
    assert report["mode"] == "dry-run"
    names = {c["name"] for c in report["checks"]}
    assert "structure.no_budgeted_code_task" in names
    assert "structure.quality_loop_module" in names
    assert report["summary"]["passed"] is True
    assert report["meta"]["create_requested"] is True


def test_live_readonly_includes_quality_surface_and_does_not_post():
    checks = gate.run_gate(_readonly_client(), project_id="boss-dogfood")
    by_name = {c.name: c for c in checks}
    assert by_name["structure.quality_loop_module"].ok is True
    assert by_name["structure.scorecard_v11_ui_ux"].ok is True
    assert by_name["structure.factory_endpoints_remain"].ok is True
    assert by_name["structure.quality_loop_contract"].ok is True
    assert by_name["structure.no_budgeted_code_task"].ok is True
    report = gate.build_report(
        api_url="u",
        project_id="p",
        checks=checks,
        started_at="a",
        finished_at="b",
        duration_ms=1,
        mode="live+readonly",
    )
    assert report["summary"]["passed"] is True


AGENT_PROCESS_EXPORTS = (
    "validate_process_spec",
    "review_prd",
    "improve_prd",
    "AgentProcessService",
)


def test_agent_process_module_exists_and_exports():
    inspection = gate.inspect_agent_process()
    assert inspection["exists"] is True
    assert inspection["path"].endswith("engine/boss_engine/services/agent_process.py")
    for name in AGENT_PROCESS_EXPORTS:
        assert inspection["exports"][name] is True, name


def test_process_compiler_and_manager_skip_honestly_when_absent():
    compiler = gate.inspect_optional_process_module("process_compiler")
    manager = gate.inspect_optional_process_module("process_manager")
    for item in (compiler, manager):
        assert item["skipped"] is (not item["exists"])
    # These modules now exist on main after the 377/378 merge. Gate must
    # report them present — not invent a skip.
    assert compiler["exists"] is True
    assert manager["exists"] is True


def test_optional_process_module_skips_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    missing = gate.inspect_optional_process_module("process_compiler")
    assert missing["exists"] is False
    assert missing["skipped"] is True


def test_scorecard_honesty_does_not_invent_a11y_or_unknown_jtbd():
    honesty = gate.inspect_scorecard_honesty()
    assert honesty["invents_a11y_violations"] is False
    assert honesty["invents_contrast_ok"] is False
    assert honesty["unknown_jtbd_not_scored"] is True


def test_quality_surface_includes_process_and_honesty_checks():
    checks = gate.quality_surface_checks()
    by_name = {c.name: c for c in checks}
    process = by_name["structure.agent_process_module"]
    assert process.ok is True
    assert process.required is True
    assert process.skipped is False
    for name in AGENT_PROCESS_EXPORTS:
        assert process.evidence["exports"][name] is True

    compiler = by_name["structure.process_compiler_module"]
    manager = by_name["structure.process_manager_module"]
    assert compiler.ok is True
    assert manager.ok is True
    assert compiler.required is False
    assert manager.required is False
    assert compiler.skipped is False
    assert manager.skipped is False
    assert compiler.evidence.get("exists") is True
    assert manager.evidence.get("exists") is True

    honesty = by_name["structure.scorecard_honesty"]
    assert honesty.ok is True
    assert honesty.evidence["invents_a11y_violations"] is False
    assert honesty.evidence["invents_contrast_ok"] is False
    assert honesty.evidence["unknown_jtbd_not_scored"] is True


def test_inspect_process_and_honesty_fall_back_when_import_fails(monkeypatch):
    monkeypatch.setattr(gate, "_try_import_engine", lambda _name: None)
    inspection = gate.inspect_agent_process()
    assert inspection["imported"] is False
    assert inspection["exists"] is True
    for name in AGENT_PROCESS_EXPORTS:
        assert inspection["exports"][name] is True, name
    honesty = gate.inspect_scorecard_honesty()
    assert honesty["imported"] is False
    assert honesty["invents_a11y_violations"] is False
    assert honesty["invents_contrast_ok"] is False
    assert honesty["unknown_jtbd_not_scored"] is True


def test_process_handoff_module_and_simulated_never_spends():
    inspection = gate.inspect_process_handoff()
    assert inspection["exists"] is True
    assert inspection["exports"]["AgentProcessService"] is True
    assert inspection["exports"]["PRD_TO_ACCEPTED_SPEC"] is True
    assert inspection["endpoints"]["/v1/agent-processes/runs"] is True
    assert inspection["endpoints"]["/v1/agent-processes/tick-all"] is True
    assert "captain_accept" in inspection["steps"]
    assert inspection["simulated_no_spend"] is True


def test_sealed_contract_fails_closed_without_decision_or_evidence():
    sealed = gate.inspect_sealed_contract()
    assert sealed["exists"] is True
    assert sealed["receipt_fail_closed"] is True
    assert sealed["decision_hold_fail_closed"] is True
    assert sealed["evidence_attach_fail_closed"] is True
    assert sealed["factory_wires_receipt"] is True
    assert sealed["factory_wires_decision"] is True
    assert sealed["governed_passes_evidence"] is True
    for name in gate.SEALED_CONTRACT_EXPORTS:
        assert sealed["exports"][name] is True, name
