"""RB-14 (meta-eval) + RB-20 (oncall notify + investigation bundle + MCP server plumbing)."""
import json

from apps.api.app.config import get_settings
from apps.api.app.db import begin
from apps.api.app.services import oncall_notify as on
from apps.api.app.services.reliability import ReliabilityService
from apps.api.app.services.reliability_meta_eval import run_meta_eval

PID = "boss-dogfood"
ACTOR = "executive_console"
RS = ReliabilityService()


def _errors(service, n):
    return [{"ts": i, "message": f"{service} boom {i}", "service": service, "level": "error"}
            for i in range(n)]


# --- RB-14 meta-eval ---------------------------------------------------------
def test_meta_eval_scores_the_agent():
    rep = run_meta_eval()
    det, rca = rep["detection"], rep["rca"]
    # the agent must catch every injected anomaly on the corpus, with high precision
    assert det["recall"] == 1.0, rep["per_scenario"]
    assert det["precision"] >= 0.8
    assert det["f1"] >= 0.9
    # and rank the right root cause first on every RCA case
    assert rca["top1_accuracy"] == 1.0, rep["per_scenario"]
    assert len(rep["per_scenario"]) == 7                       # 4 detection + 3 rca


def test_meta_eval_http(client):
    r = client.post("/v1/reliability/meta-eval")
    assert r.status_code == 200
    assert r.json()["detection"]["recall"] == 1.0


# --- RB-20 oncall card -------------------------------------------------------
def test_format_card_contains_the_essentials():
    card = on.format_card(
        "inc_1", {"title": "HighErr", "service": "pay", "severity": "critical",
                  "description": "err rate 40%"},
        {"root_cause": "deploy 9", "confidence": 0.92, "basis": "memory", "rationale": "recurring"},
        {"tier": 0, "action": "auto_remediate", "reason": "confidence ok"},
        {"code_task_run_id": "crun_x"})
    assert "🔴" in card and "HighErr" in card and "`pay`" in card
    assert "deploy 9" in card and "0.92" in card
    assert "Tier-0" in card and "crun_x" in card and "inc_1" in card


def test_notify_best_effort(monkeypatch):
    s = get_settings()
    # no webhook -> quietly False
    monkeypatch.setattr(s, "reliability_oncall_webhook", None)
    assert on.notify_incident(s, "inc_1", {"title": "t"}) is False
    # webhook set -> posts the card
    sent = {}
    monkeypatch.setattr(s, "reliability_oncall_webhook", "https://mm/hook")
    monkeypatch.setattr(on, "_http_post_json", lambda url, body: sent.update(url=url, **body) or True)
    assert on.notify_incident(s, "inc_2", {"title": "T2", "severity": "warning"}) is True
    assert sent["url"] == "https://mm/hook" and "T2" in sent["text"]
    # a raising webhook never propagates
    def boom(url, body):
        raise OSError("down")
    monkeypatch.setattr(on, "_http_post_json", boom)
    assert on.notify_incident(s, "inc_3", {"title": "T3"}) is False


# --- RB-20 investigation bundle ----------------------------------------------
def test_investigation_bundle(client, engine):
    with begin(engine) as c:
        rep = RS.scan(c, PID, {"kind": "memory", "logs": _errors("inv-svc", 12)},
                      {"start": 0, "end": 9e9}, persist=True)
        inc_id = rep["incidents"][0]["id"]
        RS.diagnose(c, PID, inc_id)
    r = client.get(f"/v1/reliability/incidents/{inc_id}/investigation",
                   params={"project_id": PID})
    assert r.status_code == 200
    bundle = r.json()
    assert bundle["incident"]["id"] == inc_id
    assert bundle["signals"] and bundle["evidence"]
    assert "decision" in bundle and bundle["decision"]["action"] in (
        "gated_pr", "auto_remediate", "handoff")
    assert "graph" in bundle


# --- RB-20 MCP server protocol (in-process, no BOSS API needed) ---------------
def test_mcp_server_protocol(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "rmcp", "scripts/reliability_mcp_server.py")
    rmcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rmcp)

    out = []
    monkeypatch.setattr(rmcp.sys, "stdout", type("W", (), {
        "write": staticmethod(lambda s: out.append(s)), "flush": staticmethod(lambda: None)})())
    monkeypatch.setattr(rmcp, "_api", lambda m, p, b=None: {"items": [{"id": "inc_9"}]})

    class FakeIn:
        def __iter__(self):
            return iter([
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "list_incidents", "arguments": {}}}),
            ])
    monkeypatch.setattr(rmcp.sys, "stdin", FakeIn())
    rmcp.main()
    replies = [json.loads(x) for x in out]
    assert replies[0]["result"]["serverInfo"]["name"] == "boss-sre"
    tools = {t["name"] for t in replies[1]["result"]["tools"]}
    assert tools == {
        "list_incidents",
        "get_investigation",
        "diagnose_incident",
        "list_capabilities",
        "verify_incident",
        "page_incident",
    }
    assert "inc_9" in replies[2]["result"]["content"][0]["text"]
