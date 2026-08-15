"""SRE runbooks: validate + Decision before effects. Never auto-apply."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from boss_engine.services.sre_runbooks import (
    BUILTIN_RUNBOOKS,
    SreRunbookError,
    SreRunbookService,
    classify_step,
    receipt_dedupe_key,
)


PID = "proj_sre_rb"
INCIDENT = "inc_rb_1"


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def one(self):
        return self._rows[0]


class _Result:
    def __init__(self, rows=None, rowcount=1):
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self):
        return _Mappings(self._rows)


class FakeConn:
    """In-memory stand-in for the SQL the runbook service emits."""

    def __init__(self):
        self.runbooks: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.decisions: dict[str, dict] = {}
        self.pages: list[dict] = []

    def begin_nested(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split()).lower()
        params = dict(params or {})
        if sql.strip().startswith("create"):
            return _Result()
        if "insert into sre_runbooks" in sql:
            steps = params.get("steps")
            if isinstance(steps, str):
                steps = json.loads(steps)
            row = {
                "id": params["id"],
                "project_id": params.get("p") or params.get("project_id"),
                "name": params["name"],
                "steps": steps,
                "created_at": params.get("now") or datetime.now(timezone.utc),
            }
            for existing in self.runbooks.values():
                if existing["project_id"] == row["project_id"] and existing["name"] == row["name"]:
                    return _Result()
            self.runbooks[row["id"]] = row
            return _Result([row])
        if "from sre_runbooks" in sql:
            pid = params.get("p") or params.get("project_id")
            if "and id" in sql or "and name" in sql:
                if params.get("id"):
                    row = self.runbooks.get(params["id"])
                    if row and row["project_id"] == pid:
                        return _Result([row])
                if params.get("name"):
                    for row in self.runbooks.values():
                        if row["project_id"] == pid and row["name"] == params["name"]:
                            return _Result([row])
                return _Result([])
            rows = [r for r in self.runbooks.values() if r["project_id"] == pid]
            rows.sort(key=lambda r: r["name"])
            return _Result(rows)
        if "insert into sre_runbook_runs" in sql:
            row = {
                "id": params["id"],
                "project_id": params.get("p") or params.get("project_id"),
                "runbook_id": params.get("runbook_id") or params.get("rb"),
                "incident_id": params.get("incident_id") or params.get("inc"),
                "status": params.get("status") or "queued",
                "decision_id": params.get("decision_id"),
                "receipt_id": params.get("receipt_id"),
                "created_at": params.get("now") or datetime.now(timezone.utc),
            }
            self.runs[row["id"]] = row
            return _Result([row])
        if "update sre_runbook_runs" in sql:
            row = self.runs.get(params["id"])
            if row:
                for key in ("status", "decision_id", "receipt_id"):
                    if key in params:
                        row[key] = params[key]
            return _Result([row] if row else [], rowcount=1 if row else 0)
        if "from sre_runbook_runs" in sql:
            pid = params.get("p") or params.get("project_id")
            rows = [r for r in self.runs.values() if r["project_id"] == pid]
            if params.get("runbook_id"):
                rows = [r for r in rows if r["runbook_id"] == params["runbook_id"]]
            if params.get("id"):
                rows = [r for r in rows if r["id"] == params["id"]]
            return _Result(rows)
        if "insert into receipts" in sql:
            row = {
                "id": params["id"],
                "org_id": params.get("org_id"),
                "project_id": params.get("project_id"),
                "dedupe_key": params.get("dedupe_key"),
                "effect_kind": params.get("effect_kind"),
                "status": "in_progress",
                "result": None,
            }
            self.receipts[row["id"]] = row
            return _Result([row])
        if "from receipts" in sql:
            if params.get("dedupe_key"):
                for row in self.receipts.values():
                    if row.get("dedupe_key") == params["dedupe_key"]:
                        return _Result([row])
            if params.get("receipt_id"):
                row = self.receipts.get(params["receipt_id"])
                return _Result([row] if row else [])
            return _Result([])
        if "update receipts" in sql:
            row = self.receipts.get(params.get("receipt_id"))
            if row:
                if "succeeded" in sql:
                    row["status"] = "succeeded"
                if "failed" in sql:
                    row["status"] = "failed"
                if params.get("result"):
                    raw = params["result"]
                    row["result"] = json.loads(raw) if isinstance(raw, str) else raw
            return _Result()
        if "insert into decisions" in sql:
            rec = params.get("recommendation")
            if isinstance(rec, str):
                rec = json.loads(rec)
            row = {
                "id": params["id"],
                "project_id": params["project_id"],
                "status": "pending",
                "default_action": params.get("default_action") or "hold",
                "recommendation": rec or {},
            }
            self.decisions[row["id"]] = row
            return _Result([row])
        if "from decisions" in sql:
            rows = list(self.decisions.values())
            pid = params.get("project_id") or params.get("p")
            if pid:
                rows = [r for r in rows if r["project_id"] == pid]
            if params.get("status"):
                rows = [r for r in rows if r["status"] == params["status"]]
            if params.get("rb") or params.get("runbook_id"):
                wanted = params.get("rb") or params.get("runbook_id")
                rows = [r for r in rows if (r.get("recommendation") or {}).get("runbook_id") == wanted]
            if params.get("inc") or params.get("incident_id"):
                wanted = params.get("inc") or params.get("incident_id")
                rows = [r for r in rows if (r.get("recommendation") or {}).get("incident_id") == wanted]
            return _Result(rows)
        if "from projects" in sql:
            return _Result([{"org_id": "org_1", "workspace_id": "ws_1"}])
        if "insert into events" in sql:
            return _Result()
        return _Result()


def _svc(monkeypatch, conn: FakeConn) -> SreRunbookService:
    svc = SreRunbookService()

    def fake_page(self, connection, project_id, **kwargs):
        conn.pages.append({"project_id": project_id, **kwargs})
        return {
            "simulated": True,
            "notify": {"receipt_id": "rcpt_oncall", "simulated": True, "posted": False},
            "page": {"id": "ocpg_sim"},
        }

    monkeypatch.setattr(
        "boss_engine.services.sre_runbooks.OncallSreService.page",
        fake_page,
    )
    return svc


def test_classify_known_and_unknown_steps():
    assert classify_step({"action": "restart_preview"}) == "safe"
    assert classify_step({"action": "page_oncall"}) == "external"
    assert classify_step({"action": "drop_production"}) == "unknown"
    assert classify_step({"action": ""}) == "unknown"


def test_seed_templates_are_no_spend():
    names = {item["name"] for item in BUILTIN_RUNBOOKS}
    assert names == {"restart_preview", "page_oncall"}
    for item in BUILTIN_RUNBOOKS:
        for step in item["steps"]:
            assert classify_step(step) in {"safe", "external"}
            assert step["action"] in {"restart_preview", "page_oncall"}


def test_execute_without_approval_holds(monkeypatch):
    conn = FakeConn()
    svc = _svc(monkeypatch, conn)
    svc.ensure_builtins(conn, PID)
    page_book = svc.get_runbook_by_name(conn, PID, "page_oncall")
    assert page_book is not None

    result = svc.execute_run(
        conn, PID, page_book["id"], INCIDENT, actor="operator", force=False,
    )
    assert result["status"] == "held"
    assert result["decision_id"]
    decision = conn.decisions[result["decision_id"]]
    assert decision["status"] == "pending"
    assert decision["default_action"] == "hold"
    # Never auto-apply, even if the caller sets force=True.
    again = svc.execute_run(
        conn, PID, page_book["id"], INCIDENT, actor="operator", force=True,
    )
    assert again["status"] == "held"
    assert conn.decisions[again["decision_id"]]["status"] == "pending"


def test_page_oncall_dry_run_writes_receipt(monkeypatch):
    conn = FakeConn()
    svc = _svc(monkeypatch, conn)
    svc.ensure_builtins(conn, PID)
    page_book = svc.get_runbook_by_name(conn, PID, "page_oncall")

    result = svc.execute_run(
        conn, PID, page_book["id"], INCIDENT, actor="operator",
    )
    assert result["receipt_id"]
    receipt = conn.receipts[result["receipt_id"]]
    step_id = page_book["steps"][0]["id"]
    assert receipt["dedupe_key"] == receipt_dedupe_key(result["id"], step_id)
    assert receipt["dedupe_key"] == f"sre_runbook:{result['id']}:{step_id}"
    assert receipt["status"] == "succeeded"
    assert (receipt.get("result") or {}).get("simulated") is True
    assert conn.pages, "page_oncall must call the existing oncall page path"
    assert conn.pages[0].get("force_live") is False


def test_unknown_step_fail_closed(monkeypatch):
    conn = FakeConn()
    svc = _svc(monkeypatch, conn)
    book = svc.create_runbook(
        conn,
        PID,
        name="rogue_drop",
        steps=[{"id": "drop", "action": "drop_production"}],
        actor="operator",
    )
    result = svc.execute_run(
        conn, PID, book["id"], INCIDENT, actor="operator",
    )
    assert result["status"] == "failed"
    assert result.get("decision_id") in (None, "")
    assert conn.pages == []
    # Fail closed: no successful effect receipt for the unknown step.
    if result.get("receipt_id"):
        assert conn.receipts[result["receipt_id"]]["status"] != "succeeded"


def test_restart_preview_is_safe_and_does_not_page(monkeypatch):
    conn = FakeConn()
    svc = _svc(monkeypatch, conn)
    svc.ensure_builtins(conn, PID)
    book = svc.get_runbook_by_name(conn, PID, "restart_preview")
    result = svc.execute_run(
        conn, PID, book["id"], INCIDENT, actor="operator",
    )
    assert result["status"] == "completed"
    assert not result.get("decision_id")
    assert result["receipt_id"]
    receipt = conn.receipts[result["receipt_id"]]
    assert receipt["dedupe_key"] == f"sre_runbook:{result['id']}:restart"
    assert receipt["status"] == "succeeded"
    assert (receipt.get("result") or {}).get("applied") is False
    assert conn.pages == []


def test_missing_runbook_is_visible(monkeypatch):
    conn = FakeConn()
    svc = _svc(monkeypatch, conn)
    try:
        svc.execute_run(conn, PID, "srb_missing", INCIDENT, actor="operator")
    except SreRunbookError as err:
        assert err.status_code == 404
    else:
        raise AssertionError("expected SreRunbookError")
