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
    found = graph_steps._itsm_matches(None, project_id="northpeak-lending",
                                      subject_id="kgn_rule", actor_ref="t")
    assert [w["name"] for w in written] == ["INC-2088"]
    assert written[0]["kind"] == "incident" and found


def test_a_ticket_touching_other_code_is_not_matched(monkeypatch):
    monkeypatch.setattr(mcp_registry, "find_mcp_server", lambda c, p, name: {"id": "itsm", "enabled": True})
    monkeypatch.setattr(mcp_registry, "call_tool_unattended",
                        lambda settings, server, tool, args: mcp_itsm_mock.TOOL_FNS[tool](args))
    monkeypatch.setattr(graph_steps._STORE, "neighbors",
                        lambda c, project_id, node_id, direction: [{"name": "ChargebackPosting"}])
    monkeypatch.setattr(graph_steps._STORE, "upsert_node", lambda c, **kw: "x")
    assert graph_steps._itsm_matches(None, project_id="p", subject_id="s", actor_ref="t") == {}
