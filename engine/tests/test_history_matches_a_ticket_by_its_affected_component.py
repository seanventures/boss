"""Historical Pattern finds a past ticket by the code it touched (2026-10-31 dry run).

NorthPeak Lending's repayment rule connects to `LoanTransactionType.isRepaymentType`; INC-2088's
title and summary never name a class, so matching on its text alone missed it and the case ran
with no history. A real ticket records its affected components; the step matches on those too."""
from __future__ import annotations

from tools import mcp_itsm_mock

from boss_engine.services import mcp_registry
from boss_engine.services.knowledge import graph_steps


def test_inc_2088_is_found_through_its_affected_component(monkeypatch):
    written = []
    monkeypatch.setattr(mcp_registry, "find_mcp_server", lambda c, p, name: {"id": "itsm", "enabled": True})
    monkeypatch.setattr(mcp_registry, "call_tool_unattended",
                        lambda settings, server, tool, args: mcp_itsm_mock.TOOL_FNS[tool](args))
    monkeypatch.setattr(graph_steps._STORE, "neighbors",
                        lambda c, project_id, node_id, direction: [
                            {"name": "LoanTransactionType.isRepaymentType"},
                            {"name": "Repayment-like transaction classification"}])

    def upsert(c, **kw):
        written.append(kw)
        return "kgn_" + kw["node_key"]

    monkeypatch.setattr(graph_steps._STORE, "upsert_node", upsert)
    edges = []
    monkeypatch.setattr(graph_steps._STORE, "upsert_edge", lambda c, **kw: edges.append(kw))
    monkeypatch.setattr(graph_steps, "incident_code",
                        lambda c, project_id, component: [f"kgn_sym_{component}"])
    found = graph_steps._itsm_matches(None, project_id="northpeak-lending",
                                      subject_id="kgn_rule", actor_ref="t")
    assert [w["name"] for w in written] == ["INC-2088"]
    assert written[0]["kind"] == "incident" and found
    # The incident is linked to the code its affected components name (2026-10-31: the
    # allocation code was otherwise never read by the assessment or the lanes).
    assert {(e["kind"], e["dst_id"]) for e in edges} == {
        ("found_in", "kgn_sym_LoanTransactionType.isRepaymentType"),
        ("found_in", "kgn_sym_AcmeLoanRepaymentScheduleTransactionProcessor")}


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_a, **_k):
        return _Rows(self.rows)


def test_an_incident_is_never_linked_to_test_code():
    conn = _Conn([
        {"id": "kgn_test", "node_key": "symbol:o/r:fineract-e2e-tests-core/src/test/java/X.java:X"},
        {"id": "kgn_main", "node_key": "symbol:o/r:fineract-loan/src/main/java/LoanPaymentAllocationRule.java:LoanPaymentAllocationRule"},
    ])
    assert graph_steps.incident_code(conn, project_id="p",
                                     component="LoanPaymentAllocationRule") == ["kgn_main"]


def test_a_ticket_touching_other_code_is_not_matched(monkeypatch):
    monkeypatch.setattr(mcp_registry, "find_mcp_server", lambda c, p, name: {"id": "itsm", "enabled": True})
    monkeypatch.setattr(mcp_registry, "call_tool_unattended",
                        lambda settings, server, tool, args: mcp_itsm_mock.TOOL_FNS[tool](args))
    monkeypatch.setattr(graph_steps._STORE, "neighbors",
                        lambda c, project_id, node_id, direction: [{"name": "ChargebackPosting"}])
    monkeypatch.setattr(graph_steps._STORE, "upsert_node", lambda c, **kw: "x")
    assert graph_steps._itsm_matches(None, project_id="p", subject_id="s", actor_ref="t") == {}
