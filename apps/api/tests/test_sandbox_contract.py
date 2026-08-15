"""#99 — the versioned async Python⇄Go sandbox contract: request build, response parsing (defensive),
status/terminal semantics, validation, and the golden-rule availability gate."""
from types import SimpleNamespace

from apps.api.app.services import sandbox_contract as sc


def test_version_and_statuses():
    assert sc.CONTRACT_VERSION == "1.0"
    assert sc.is_terminal("done") and sc.is_terminal("failed") and sc.is_terminal("timeout")
    assert sc.is_terminal("cancelled")
    assert not sc.is_terminal("running") and not sc.is_terminal("queued") and not sc.is_terminal(None)
    assert sc.succeeded("done") and not sc.succeeded("failed")


def test_available_gates_the_contract():
    assert sc.available(SimpleNamespace(sandbox_agent_url="http://svc:8077")) is True
    assert sc.available(SimpleNamespace(sandbox_agent_url="")) is False   # -> engine falls back to local
    assert sc.available(SimpleNamespace()) is False


def test_routes():
    assert sc.submit_path() == "/v1/sandbox/tasks"
    assert sc.status_path("sid1") == "/v1/sandbox/tasks/sid1"
    assert sc.release_path("sid1") == "/v1/sandbox/tasks/sid1"
    assert sc.cancel_path("sid1") == "/v1/sandbox/tasks/sid1/cancel"
    assert sc.heartbeat_path("sid1") == "/v1/sandbox/tasks/sid1/heartbeat"


def test_build_submit_request():
    spec = {"owner": "paramg", "repo": "boss-sandbox", "instructions": "do X",
            "acceptance_criteria": ["a"], "test_commands": ["pytest"], "engine": "claude_code",
            "claude_oauth_token": "tok", "openai_api_key": "sk"}
    body = sc.build_submit_request(spec, "tenant-1", task_id="crun_9")
    assert body["contract_version"] == "1.0" and body["task_id"] == "crun_9"
    assert body["tenant_id"] == "tenant-1"
    assert body["repo"] == {"owner": "paramg", "name": "boss-sandbox",
                            "base_branch": "main", "work_branch": "boss/code/crun_9"}
    assert body["engine"] == "claude_code"
    # creds map into engine_env by env-var name, never baked elsewhere
    assert body["engine_env"] == {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "OPENAI_API_KEY": "sk"}


def test_parse_submit_response():
    assert sc.parse_submit_response({"sandbox_task_id": "sid1"}) == "sid1"
    assert sc.parse_submit_response({"id": "sid2"}) == "sid2"
    assert sc.parse_submit_response({}) is None
    assert sc.parse_submit_response("garbage") is None


def test_parse_status_is_defensive():
    assert sc.parse_status({"status": "running"})["terminal"] is False
    done = sc.parse_status({"status": "done", "phase": "review"})
    assert done["terminal"] and done["succeeded"] and done["phase"] == "review"
    # unknown/empty status -> RUNNING (keep polling), never a false terminal
    assert sc.parse_status({"status": "weird"})["status"] == "running"
    assert sc.parse_status("nope")["status"] == "running"


def test_parse_result_extracts_diff_and_flags():
    resp = {"status": "done", "result": {
        "diff": [{"path": "a.py", "content": "x"}, {"bad": "no path"}],
        "tests_passed": True, "summary": "did it", "cost_usd_micros": 1234}}
    r = sc.parse_result(resp)
    assert r["diff"] == [{"path": "a.py", "content": "x"}]     # malformed file entry dropped
    assert r["tests_passed"] is True and r["summary"] == "did it" and r["cost_usd_micros"] == 1234
    assert sc.parse_result("junk")["diff"] == []               # never crashes


def test_validate_submit_request():
    good = sc.build_submit_request({"owner": "o", "repo": "r"}, None, task_id="t1")
    assert sc.validate_submit_request(good) == []
    bad = sc.validate_submit_request({"contract_version": "0.9", "repo": {}})
    assert any("contract_version" in p for p in bad)
    assert any("task_id" in p for p in bad) and any("repo.owner" in p for p in bad)


def test_provider_submit_body_uses_the_contract():
    # MicroVMProvider._submit_body now delegates to the versioned builder
    from apps.api.app.services.sandbox_provider import MicroVMProvider
    p = MicroVMProvider(SimpleNamespace(sandbox_token="", sandbox_agent_url="http://svc"))
    body = p._submit_body({"owner": "o", "repo": "r", "task_id": "crun_x"}, "t")
    assert body["contract_version"] == "1.0" and body["task_id"] == "crun_x"
