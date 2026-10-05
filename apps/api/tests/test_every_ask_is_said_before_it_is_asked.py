"""Every time the operator will be asked is said before they start — by the server, truthfully.

The composer listed approvals from its own copy of the rules: it never mentioned the merge
approval every build raises, and it promised the panel's PRD is approved automatically, which
is a setting (BOSS_MEETING_PRD_AUTO_APPROVE). GET /v1/delivery-workflows/asks answers from the
rules the runtime acts on, and the catalogue's gate counts read the same function.

The run test starts a real Minor Feature onto an app, lets the real panel convene and the real
finalizer raise the PRD decision (only the panel's model phases are skipped: the PRD is written
where they write it), ticks the runner to the build, and compares what was actually asked with
what /asks said would be.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.tests.test_a_feature_builds_onto_its_app import (
    APP_RUN, _start, _the_app, _tick_until_built,
)


def _project() -> str:
    return get_settings().default_project_id


def _install(client):
    installed = client.post("/v1/delivery-workflows/catalogue/install",
                            json={"project_id": _project(), "actor": "ceo"})
    assert installed.status_code == 200, installed.text


def _asks(client) -> dict:
    got = client.get("/v1/delivery-workflows/asks", params={"project_id": _project()})
    assert got.status_code == 200, got.text
    return got.json()


def _shape(asks):
    return [(a["kind"], a["who"]) for a in asks]


def test_asks_names_the_merge_and_reads_the_prd_setting(client, monkeypatch):
    from apps.api.app import main as api_main

    _install(client)
    body = _asks(client)
    assert body["project_id"] == _project() and body["prd_auto_approve"] is True
    minor = body["workflows"]["minor_feature"]
    assert [(a["step_id"], a["kind"], a["who"]) for a in minor] == [
        ("meeting", "spec", "boss"), ("build", "merge", "you"), ("deploy", "gate", "you")]
    assert all(a["label"] for a in minor)
    # Every workflow that builds asks for its merge, once.
    for key, asks in body["workflows"].items():
        assert sum(1 for a in asks if a["kind"] == "merge") <= 1, key
    assert _shape(body["routes"]["@quick"]) == [("gate", "you"), ("merge", "you")]
    assert _shape(body["routes"]["@spec"]) == [("spec", "boss"), ("merge", "you")]

    # A person signs the PRD when the setting says so — everywhere at once.
    monkeypatch.setattr(api_main.settings, "meeting_prd_auto_approve", False)
    off = _asks(client)
    assert off["prd_auto_approve"] is False
    assert off["workflows"]["minor_feature"][0]["who"] == "you"
    assert off["routes"]["@spec"][0]["who"] == "you"
    catalogue = {i["key"]: i for i in client.get("/v1/delivery-workflows/catalogue").json()["items"]}
    assert "spec_review" in catalogue["standard_product_delivery"]["gates"]
    assert "spec_review" not in catalogue["standard_product_delivery"]["automatic_approvals"]

    monkeypatch.setattr(api_main.settings, "meeting_prd_auto_approve", True)
    catalogue = {i["key"]: i for i in client.get("/v1/delivery-workflows/catalogue").json()["items"]}
    assert "spec_review" not in catalogue["standard_product_delivery"]["gates"]
    assert "spec_review" in catalogue["standard_product_delivery"]["automatic_approvals"]


def _real_panel(monkeypatch, engine, *, auto_approve: bool):
    """The meeting step's real tool convenes the real panel; its model phases are replaced by
    the PRD they would write, and the real finalizer raises the PRD decision."""
    from boss_engine.services import workflow_capability_runner as runner
    from boss_engine.services.meeting_service import _finalize_meeting

    real = runner._invoke_tool
    settings = get_settings().model_copy(update={
        "meeting_prd_auto_approve": auto_approve,
        # Not what is tested here, and it would start a second build in a thread.
        "auto_code_task_after_prd": False})
    held: list[str] = []

    def invoke(ctx, tool_name, project_id, actor, args):
        result = real(ctx, tool_name, project_id, actor, args)
        if tool_name == "request_meeting":
            run_id = result["meeting"]["run_id"]
            held.append(run_id)
            with engine.begin() as connection:
                connection.execute(text(
                    "UPDATE meeting_runs SET state = jsonb_build_object('prd_markdown', "
                    "'# PRD' || chr(10) || 'A Download CSV button.') WHERE id = :id"),
                    {"id": run_id})
                _finalize_meeting(connection, settings, run_id)
        return result

    monkeypatch.setattr(runner, "_invoke_tool", invoke)
    return held


def _asked_for_run(engine, meeting_run_id: str, workflow_run_id: str) -> list[tuple[str, str]]:
    """(kind, who) for every decision this run actually raised, in the order raised."""
    with engine.begin() as connection:
        rows = connection.execute(text(
            "SELECT d.created_at, d.recommendation, d.status, d.approved_by "
            "FROM decisions d WHERE d.id IN ("
            "  SELECT decision_id FROM meeting_runs WHERE id = :m"
            "  UNION SELECT decision_id FROM workflow_step_runs WHERE workflow_run_id = :w"
            "  UNION SELECT decision_id FROM code_task_runs "
            "        WHERE finalize_ctx->>'workflow_run_id' = :w) "
            "ORDER BY d.created_at"), {"m": meeting_run_id, "w": workflow_run_id}
        ).mappings().all()
    out = []
    for row in rows:
        action = (row["recommendation"] or {}).get("action")
        tool = (row["recommendation"] or {}).get("tool")
        kind = ("spec" if tool == "publish_document" else
                "merge" if action == "review_pull_request" else "gate")
        who = "boss" if row["approved_by"] == "auto_approval" else "you"
        out.append((kind, who))
    return out


def _drive(client, engine, monkeypatch, *, auto_approve: bool):
    _the_app(engine)
    _install(client)
    held = _real_panel(monkeypatch, engine, auto_approve=auto_approve)
    started = _start(client, {"title": "Export swaps as CSV",
                              "directive": "Add a Download CSV button to the approvals page.",
                              "extends_run_id": APP_RUN})
    assert started.status_code == 200, started.text
    workflow_run_id = started.json()["run"]["id"]
    built = _tick_until_built(client, engine, workflow_run_id)
    assert held, "the meeting step never convened the panel"
    return workflow_run_id, held[0], built


def test_what_a_run_asks_is_what_asks_said(client, engine, monkeypatch):
    from apps.api.app import main as api_main

    monkeypatch.setattr(api_main.settings, "meeting_prd_auto_approve", True)
    workflow_run_id, meeting_run_id, built = _drive(client, engine, monkeypatch,
                                                    auto_approve=True)
    promised = _shape(_asks(client)["workflows"]["minor_feature"])
    asked = _asked_for_run(engine, meeting_run_id, workflow_run_id)
    # Up to the build, what was asked is exactly the head of the promise: the PRD, by BOSS.
    assert asked == promised[:len(asked)] == [("spec", "boss")]
    # The next promised ask is the build's merge, from the code task the build step started.
    assert promised[len(asked)] == ("merge", "you")
    assert len(built) == 1


def test_with_the_prd_gate_on_the_run_asks_you_as_promised(client, engine, monkeypatch):
    from apps.api.app import main as api_main

    monkeypatch.setattr(api_main.settings, "meeting_prd_auto_approve", False)
    _the_app(engine)
    _install(client)
    held = _real_panel(monkeypatch, engine, auto_approve=False)
    started = _start(client, {"title": "Export swaps as CSV",
                              "directive": "Add a Download CSV button to the approvals page.",
                              "extends_run_id": APP_RUN})
    assert started.status_code == 200, started.text
    for _ in range(3):
        client.post("/v1/delivery-workflows/run-capabilities", json={"project_id": _project()})
    assert held
    promised = _shape(_asks(client)["workflows"]["minor_feature"])
    asked = _asked_for_run(engine, held[0], started.json()["run"]["id"])
    assert asked == promised[:1] == [("spec", "you")]
