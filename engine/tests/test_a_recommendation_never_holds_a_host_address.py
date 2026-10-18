"""A BOSS recommendation never holds a host address, and nothing bound upstream does either.

THE FINDING (BOSSAPP-571, spec §7.3): the two real `boss_recommendations` rows of run
wfr_f383649a8b024d97a80c held the deploy host's public address in their symptom and evidence,
because `redact_for_agent` removes credentials and keeps addresses. A recommendation seeds a
self-fix, and a self-fix's evidence is what a person may send to the BOSS team.

The class, not the instance: every address shape (IPv4, IPv6, URL hosts, `user@host`, dotted
host names, this install's own machines by bare name) is masked when a recommendation is written
AND when one is read (a row written before the rule), in the API's list and in
RECOMMENDATIONS.md. What a diagnosis needs — files, functions, containers, ports, `0.0.0.0` —
stays. Placeholders only (RFC 5737 / RFC 3849 / example.com): no real address is written here.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import boss_recommendations
from boss_engine.services.self_fix_privacy import findings, mask_hosts, mask_value

from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

ADDRESS = "203.0.113.10"


@pytest.mark.parametrize("raw, gone", [
    (f"Port 8000 on {ADDRESS} is already held by another container", ADDRESS),
    (f"ssh deploy@{ADDRESS} 'docker ps'", ADDRESS),
    ("health probe http://198.51.100.7:8000/health timed out", "198.51.100.7"),
    ("https://helpdesk.example.com/api answered 502", "helpdesk.example.com"),
    ("could not reach db-01.internal on 5432", "db-01.internal"),
    ("bound on 2001:db8::1 port 443", "2001:db8::1"),
    ("ssh deploy@edge-box-01 refused", "edge-box-01"),
])
def test_every_address_shape_is_masked(raw, gone):
    masked = mask_hosts(raw)
    assert gone not in masked
    assert "[ip]" in masked or "[host]" in masked


@pytest.mark.parametrize("kept", [
    "Bind for 0.0.0.0:8000 failed: port is already allocated",
    "engine/boss_engine/services/stack_deploy.py: retire_own_projects",
    "container bossapp-example-app-deploy-app-1 (project bossapp-example-app-deploy)",
    "getaddrinfo EAI_AGAIN res-db",
    "BOSS 1.0.0-rc10.7+0123456789ab at 12:30:45",
    "listen on 127.0.0.1:5432 and ::1",
])
def test_what_a_diagnosis_needs_stays(kept):
    assert mask_hosts(kept) == kept


def test_this_installs_own_machines_are_masked_by_bare_name():
    said = "the deploy to edgebox failed; edgebox answered nothing"
    assert "edgebox" not in mask_hosts(said, known=["edgebox"])
    assert mask_hosts("edgeboxes are fine", known=["edgebox"]) == "edgeboxes are fine"


def test_a_check_reports_without_changing_the_text():
    code = 'HOST = "203.0.113.10"\nPORT = 8000\n'
    assert findings(code) == ["line 1: [ip]"]
    assert findings("PORT = 8000\n") == []


def test_nested_evidence_is_masked_and_keys_are_kept():
    value = {"symptom": f"down on {ADDRESS}", "lines": [f"curl http://{ADDRESS}/x"], "port": 8000}
    masked = mask_value(value)
    assert ADDRESS not in json.dumps(masked) and masked["port"] == 8000


# ── the table, the API's list and RECOMMENDATIONS.md ─────────────────────────────────────────

def test_a_recommendation_is_written_and_read_without_an_address(eng, project, tmp_path):
    host_id = f"dh_{uuid.uuid4().hex[:10]}"

    class _Settings:
        artifact_root = tmp_path

    with eng.begin() as c:
        c.execute(text("INSERT INTO deployment_hosts (id, project_id, name, address) "
                       "VALUES (:i, :p, 'edgebox', :a)"),
                  {"i": host_id, "p": project, "a": ADDRESS})
        out = boss_recommendations.record(
            c, _Settings(), project_id=project, signature="deploy:port_held:8000",
            title=f"Port 8000 on {ADDRESS} held by an old release",
            symptom=f"Port 8000 on {ADDRESS} (edgebox) is already held",
            evidence=[f"Bind for 0.0.0.0:8000 failed on deploy@{ADDRESS}",
                      "edgebox: bossapp-example-app-deploy-app-1 holds 8000"],
            root_cause="retire_own_projects never names the shared project",
            proposed_change=f"retire it; see http://{ADDRESS}:8000/health",
            files=["engine/boss_engine/services/stack_deploy.py"],
            ref={"run_id": "wfr_test", "step_id": "deploy"}, actor="test")
        assert out["created"]
        raw = c.execute(text("SELECT title, symptom, evidence, proposed_change FROM "
                             "boss_recommendations WHERE id = :i"), {"i": out["id"]}).first()
        # AN OLD ROW, written before the rule: it is masked on read.
        old = f"brec_{uuid.uuid4().hex[:10]}"
        c.execute(text("INSERT INTO boss_recommendations (id, project_id, signature, title, "
                       "symptom, evidence) VALUES (:i, :p, 'deploy:unwired:res-db', :t, :s, "
                       "CAST(:e AS JSONB))"),
                  {"i": old, "p": project, "t": f"res-db unreachable on {ADDRESS}",
                   "s": f"getaddrinfo EAI_AGAIN res-db on {ADDRESS}",
                   "e": json.dumps([f"ssh deploy@{ADDRESS}"])})
        listed = boss_recommendations.list_recommendations(c, project)
        md = boss_recommendations.markdown(listed)
        c.execute(text("DELETE FROM boss_recommendations WHERE project_id = :p"), {"p": project})
        c.execute(text("DELETE FROM deployment_hosts WHERE id = :i"), {"i": host_id})
    stored = json.dumps([str(v) for v in raw])
    assert ADDRESS not in stored and "edgebox" not in stored
    assert "0.0.0.0:8000" in stored and "bossapp-example-app-deploy-app-1" in stored
    served = json.dumps(listed)
    assert ADDRESS not in served and ADDRESS not in md
    assert "stack_deploy.py" in md and "res-db" in md
