"""Workflows, Infra & Models, Settings.

These three are "what this company is CONFIGURED with", and the discipline is the same
throughout: show what is really there, say plainly what is not, and never render a control that
cannot act.

The artifact's demo data promises more than this deployment has — versioned workflows, fleet
load percentages, SSO, human seats, listable API keys. Every one of those is reported as absent
WITH a reason, because a settings page that renders an empty SSO form implies SSO is
configurable here and merely unconfigured. That is a different and false claim.
"""

from datetime import datetime, timezone

import pytest

from boss_engine.services.console_v2_platform import infra, settings, workflows

NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)


class FakeConn:
    def __init__(self, **tables):
        self.tables = tables
        self.statements = []

    def execute(self, statement, params=None):
        raw = str(statement)
        self.statements.append((raw, dict(params or {})))
        low = raw.lower()
        if "agent_process_templates" in low:
            rows = self.tables.get("templates", [])
        elif "deployment_hosts" in low:
            rows = self.tables.get("hosts", [])
        elif "deployment_targets" in low:
            rows = self.tables.get("targets", [])
        elif "deployment_runs" in low:
            rows = self.tables.get("deploys", [])
        elif "llm_calls" in low:
            rows = self.tables.get("models", [])
        elif "mcp_servers" in low:
            rows = self.tables.get("servers", [])
        elif "from roles" in low:
            rows = self.tables.get("roles", [])
        else:
            rows = self.tables.get("repos", [])
        captured = rows

        class _R:
            def mappings(self):
                return self

            def all(self):
                return [dict(r) for r in captured]

        return _R()


# --------------------------------------------------------------------------- #
# Workflows
# --------------------------------------------------------------------------- #
def test_a_workflow_lists_its_stages():
    out = workflows(FakeConn(templates=[{
        "id": "tpl_1", "process_key": "prd_to_ship", "name": "PRD to ship",
        "description": "Panel, build, review, ship.", "status": "active",
        "created_by": "ceo", "updated_at": NOW,
        "spec": {"steps": [
            {"id": "intake", "action": "accept_prd", "role": "cpo"},
            {"id": "build", "action": "build", "role": "coder", "gated": True},
        ]},
        "runs_30d": 4, "running_now": 1, "last_run_at": NOW,
    }]), "p")
    wf = out["workflows"][0]
    assert [s["key"] for s in wf["stages"]] == ["intake", "build"]
    assert wf["runs_30d"] == 4 and wf["running_now"] == 1


def test_a_gated_stage_is_marked():
    """A stage that stops for a human is the one that matters on this screen."""
    out = workflows(FakeConn(templates=[{
        "id": "t", "process_key": "k", "name": "n", "description": "", "status": "active",
        "created_by": "x", "updated_at": NOW, "runs_30d": 0, "running_now": 0, "last_run_at": None,
        "spec": {"steps": [{"id": "a", "action": "x", "gated": False},
                           {"id": "b", "action": "y", "requires_approval": True}]},
    }]), "p")
    assert [s["gated"] for s in out["workflows"][0]["stages"]] == [False, True]


def test_versions_are_declared_absent_rather_than_faked():
    """The artifact offers 'Deploy version' and a version number. BOSS keeps one active spec
    per process, so a version control would be a button that does nothing."""
    out = workflows(FakeConn(templates=[]), "p")
    assert out["versioned"] is False
    assert "no versions" in out["version_note"].lower()


def test_a_malformed_stage_is_skipped_not_crashed():
    """A spec written by hand or by an older version must not take the screen down."""
    out = workflows(FakeConn(templates=[{
        "id": "t", "process_key": "k", "name": "n", "description": "", "status": "active",
        "created_by": "x", "updated_at": NOW, "runs_30d": 0, "running_now": 0, "last_run_at": None,
        "spec": {"steps": ["just a string", {"id": "ok", "action": "a"}]},
    }]), "p")
    assert [s["key"] for s in out["workflows"][0]["stages"]] == ["ok"]


# --------------------------------------------------------------------------- #
# Infra
# --------------------------------------------------------------------------- #
def test_only_a_verified_host_is_marked_verified():
    """An unverified host offered as a destination fails at deploy time, far from the choice
    that caused it."""
    out = infra(FakeConn(hosts=[
        {"id": "h1", "name": "reference host", "address": "10.0.0.1", "arch": "arm64",
         "status": "verified", "labels": {}, "created_at": NOW},
        {"id": "h2", "name": "spare", "address": "10.0.0.2", "arch": "x86",
         "status": "pending", "labels": {}, "created_at": NOW},
    ]), "p")
    assert [h["verified"] for h in out["hosts"]] == [True, False]


def test_fleet_load_is_absent_rather_than_invented():
    """The artifact shows load percentages. BOSS records verification, not utilisation, and a
    fabricated 62% would be indistinguishable from a real one — this console has already
    shipped one number that was quietly made up."""
    # Seeded, because an empty host list makes the per-host assertion vacuous — it passed
    # against a version that DID invent a load, which is the failure mode of a test written
    # against no data.
    out = infra(FakeConn(hosts=[
        {"id": "h1", "name": "reference host", "address": "10.0.0.1", "arch": "arm64",
         "status": "verified", "labels": {}, "created_at": NOW},
    ]), "p")
    assert out["load_recorded"] is False
    assert "not utilisation" in out["load_note"]
    assert out["hosts"], "seed a host or this proves nothing"
    for host in out["hosts"]:
        assert "load" not in host, f"invented a load for {host['name']}"
        assert "utilisation" not in host


def test_model_spend_is_attributed_because_a_call_records_its_own_model():
    """Unlike per-agent spend, which BOSS does not record and which this console therefore
    does not show."""
    out = infra(FakeConn(models=[
        {"model": "claude-opus-5", "calls": 12, "micros": 4_500_000,
         "tokens": 90_000, "any_estimated": False},
    ]), "p")
    assert out["models"][0]["spend_usd"] == 4.5
    assert out["models"][0]["estimated"] is False


def test_an_estimated_price_is_flagged():
    """A price the price-map did not know is a guess, and it must not sit beside measured
    figures looking like one."""
    out = infra(FakeConn(models=[
        {"model": "some-new-model", "calls": 3, "micros": 1_000,
         "tokens": 10, "any_estimated": True},
    ]), "p")
    assert out["models"][0]["estimated"] is True


def test_a_retired_target_is_marked_retired():
    out = infra(FakeConn(targets=[
        {"id": "t1", "app_name": "Shorty", "repo": "example-user/x", "environment": "staging",
         "target_kind": "host", "target_ref": "h1", "status": "retired"},
    ]), "p")
    assert out["targets"][0]["retired"] is True


def test_integrations_carry_their_risk_and_boundary():
    """An MCP server's risk tier and data boundary decide what it may touch. Listing the name
    alone would make a tier-3 external server look like a tier-1 internal one."""
    out = infra(FakeConn(servers=[
        {"id": "s1", "name": "github", "url": "https://x", "transport": "http",
         "enabled": True, "risk_tier": 2, "data_boundary": "external"},
    ]), "p")
    assert out["integrations"][0]["risk_tier"] == 2
    assert out["integrations"][0]["data_boundary"] == "external"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_what_this_deployment_does_not_have_is_named_with_a_reason():
    """An empty SSO form implies SSO is configurable here and merely unconfigured. It is not."""
    out = settings(FakeConn(), "p")
    ids = {u["id"] for u in out["unavailable"]}
    assert {"sso", "members", "api_keys"} <= ids
    for entry in out["unavailable"]:
        assert len(entry["reason"]) > 30, entry


def test_roles_are_agent_roles_and_the_screen_says_so():
    """BOSS has agent roles, not human seats. Presenting roles under a 'Members' heading would
    suggest people can be invited."""
    out = settings(FakeConn(roles=[
        {"role_key": "cto", "display_name": "CTO", "autonomy_level": 2, "trust_score": 60},
    ]), "p")
    assert out["roles"][0]["key"] == "cto"
    members = [u for u in out["unavailable"] if u["id"] == "members"][0]
    assert "AGENT roles, not human seats" in members["reason"]


def test_repos_come_from_what_code_tasks_actually_target():
    """Not a configured list — the repos work has actually gone to."""
    out = settings(FakeConn(repos=[
        {"owner": "example-user", "repo": "boss-sandbox", "runs": 40, "prs": 12, "last_run_at": NOW},
    ]), "p")
    assert out["repos"][0]["name"] == "example-user/boss-sandbox"
    assert out["repos"][0]["prs"] == 12


def test_a_repo_with_missing_parts_still_renders():
    out = settings(FakeConn(repos=[
        {"owner": None, "repo": "orphan", "runs": 1, "prs": 0, "last_run_at": None},
    ]), "p")
    assert out["repos"][0]["name"] == "?/orphan"


def test_gating_is_unknown_when_the_spec_never_records_it():
    """The live template has eleven steps and not one carries a gating field.

    Reporting "0 stages stop for you" from that asserts that nothing does, when the spec simply
    does not say — BOSS's human gate lives in `decisions`, not on a step. Same absence-vs-zero
    mistake as per-agent spend and "Verified today: 100".
    """
    out = workflows(FakeConn(templates=[{
        "id": "t", "process_key": "k", "name": "n", "description": "", "status": "active",
        "created_by": "x", "updated_at": NOW, "runs_30d": 0, "running_now": 0, "last_run_at": None,
        "spec": {"steps": [{"id": "a", "action": "accept_prd", "role": "pm"},
                           {"id": "b", "action": "build", "role": "coder"}]},
    }]), "p")
    assert out["gates_recorded"] is False
    assert "do not record" in out["gates_note"]
    assert all(s["gated"] is None for s in out["workflows"][0]["stages"])


def test_gating_is_reported_when_the_spec_does_record_it():
    """One step carrying the field is enough to make False meaningful for the others."""
    out = workflows(FakeConn(templates=[{
        "id": "t", "process_key": "k", "name": "n", "description": "", "status": "active",
        "created_by": "x", "updated_at": NOW, "runs_30d": 0, "running_now": 0, "last_run_at": None,
        "spec": {"steps": [{"id": "a", "action": "x", "gated": False},
                           {"id": "b", "action": "y", "gated": True}]},
    }]), "p")
    assert out["gates_recorded"] is True
    assert out["gates_note"] == ""
    assert [s["gated"] for s in out["workflows"][0]["stages"]] == [False, True]


# --------------------------------------------------------------------------- #
# The spec is a DAG, not a list
#
# It records `kind`, `next` and `wait_for`. An earlier version read only the array order and
# rendered eleven identical lines, losing the fact that three reviews run at once and one step
# waits for all of them. The shape was in the data the whole time.
# --------------------------------------------------------------------------- #
def _template(steps, entry=None, **over):
    base = {
        "id": "t", "process_key": "k", "name": "n", "description": "",
        "status": "active", "created_by": "x", "updated_at": NOW,
        "runs_30d": 0, "running_now": 0, "last_run_at": None,
        "spec": {"entry": entry, "steps": steps},
    }
    base.update(over)
    return base


PRD_STEPS = [
    {"id": "intake", "kind": "role", "role": "pm", "action": "accept_prd",
     "next": ["rev_a", "rev_b", "rev_c"]},
    {"id": "rev_a", "kind": "role", "role": "architect", "action": "review_prd", "next": ["improve"]},
    {"id": "rev_b", "kind": "role", "role": "cto", "action": "review_prd", "next": ["improve"]},
    {"id": "rev_c", "kind": "role", "role": "vp", "action": "review_prd", "next": ["improve"]},
    {"id": "improve", "kind": "role", "role": "pm", "action": "improve_prd",
     "wait_for": ["rev_a", "rev_b", "rev_c"], "next": ["certify"]},
    {"id": "certify", "kind": "panel", "role": "architect", "action": "certify_spec"},
]


def _stages(steps, entry=None):
    out = workflows(FakeConn(templates=[_template(steps, entry)]), "p")
    return {s["key"]: s for s in out["workflows"][0]["stages"]}


def test_a_fan_out_is_parallel():
    """Three reviews that start together are not three sequential steps."""
    assert _stages(PRD_STEPS, "intake")["intake"]["kind"] == "PARALLEL"


def test_a_fan_in_is_a_join():
    """A step that waits for three others is where the branches come back together."""
    assert _stages(PRD_STEPS, "intake")["improve"]["kind"] == "JOIN"


def test_a_panel_stays_a_panel_whatever_its_edges():
    assert _stages(PRD_STEPS, "intake")["certify"]["kind"] == "PANEL"


def test_a_plain_step_is_sequential():
    assert _stages(PRD_STEPS, "intake")["rev_a"]["kind"] == "SEQUENTIAL"


def test_stages_come_back_in_execution_order_not_array_order():
    """Array order is authoring order and can differ from what runs."""
    shuffled = [PRD_STEPS[5], PRD_STEPS[4], PRD_STEPS[1], PRD_STEPS[0], PRD_STEPS[2], PRD_STEPS[3]]
    out = workflows(FakeConn(templates=[_template(shuffled, "intake")]), "p")
    keys = [s["key"] for s in out["workflows"][0]["stages"]]
    assert keys[0] == "intake"
    assert keys.index("improve") > keys.index("rev_a")


def test_a_step_unreachable_from_the_entry_is_shown_and_marked():
    """A stage that cannot run is worth seeing, not hiding."""
    steps = PRD_STEPS + [{"id": "orphan", "kind": "role", "role": "x", "action": "y"}]
    stages = _stages(steps, "intake")
    assert "orphan" in stages
    assert stages["orphan"]["unreachable"] is True
    assert stages["intake"]["unreachable"] is False


def test_a_spec_with_no_entry_still_renders_every_step():
    """Hand-written or older specs must not vanish."""
    stages = _stages(PRD_STEPS, entry=None)
    assert len(stages) == len(PRD_STEPS)


def test_the_summary_counts_are_counted_not_estimated():
    out = workflows(FakeConn(templates=[_template(PRD_STEPS, "intake")]), "p")
    wf = out["workflows"][0]
    assert len(wf["stages"]) == 6
    assert wf["panels"] == 1
    assert wf["parallel_points"] == 1
    assert wf["roles"] == ["architect", "cto", "pm", "vp"]


def test_each_stage_carries_where_control_goes_next():
    stages = _stages(PRD_STEPS, "intake")
    assert stages["intake"]["next"] == ["rev_a", "rev_b", "rev_c"]
    assert stages["improve"]["waits_for"] == ["rev_a", "rev_b", "rev_c"]


def test_the_payload_says_the_template_cannot_be_edited():
    """The reference has + Add stage, Deploy version and a remove control. BOSS has no editing
    API, so they must be absent rather than drawn as buttons that cannot act."""
    out = workflows(FakeConn(templates=[]), "p")
    assert out["editable"] is False


# --------------------------------------------------------------------------- #
# Infra: apps under the machine they run on, with the address that answers
#
# The operator compared this to v1's CI/CD screen, which shows each app, its host, its pipeline
# and a link to the running thing. v2 listed hosts and targets as two flat lists and never read
# the address at all -- so it could not say where anything was, while the app was plainly up.
# --------------------------------------------------------------------------- #
HOST = {"id": "h1", "name": "hetzner-ci-01", "address": "192.0.2.202", "arch": "AMD64",
        "status": "verified", "labels": None, "created_at": None}


def tgt(app="Invoice-Chaser", domains=("192.0.2.202:3013",), ref="root@192.0.2.202:22",
        status="active", tid="deptgt_1"):
    return {"id": tid, "app_name": app, "repo": "boss-sandbox", "environment": "staging",
            "target_kind": "ssh", "target_ref": ref, "status": status,
            "domains": list(domains)}


def test_a_target_carries_the_address_the_app_answers_on():
    out = infra(FakeConn(hosts=[HOST], targets=[tgt()]), "proj")
    t = out["targets"][0]
    assert t["url"] == "http://192.0.2.202:3013"
    assert t["domains"] == ["192.0.2.202:3013"]


def test_a_target_with_no_address_offers_no_link():
    out = infra(FakeConn(hosts=[HOST], targets=[tgt(domains=())]), "proj")
    assert out["targets"][0]["url"] is None


def test_an_app_is_grouped_under_the_host_it_deploys_to():
    """Two flat lists make the reader join host to app by eye, off an ssh string."""
    out = infra(FakeConn(hosts=[HOST], targets=[tgt()]), "proj")
    assert out["targets"][0]["host"] == "hetzner-ci-01"


def test_an_app_on_an_unregistered_host_is_not_filed_under_the_wrong_one():
    """Shown under no machine beats shown under the wrong machine."""
    out = infra(FakeConn(hosts=[HOST], targets=[tgt(ref="root@10.0.0.9:22")]), "proj")
    assert out["targets"][0]["host"] is None


def test_the_last_deploy_travels_with_its_target():
    out = infra(FakeConn(
        hosts=[HOST], targets=[tgt()],
        deploys=[{"deployment_target_id": "deptgt_1", "status": "succeeded",
                  "commit_ref": "19d5806aaaa", "finished_at": None, "failure_reason": None}],
    ), "proj")
    last = out["targets"][0]["last_deploy"]
    assert last["status"] == "succeeded"
    assert last["commit"] == "19d5806aaa"


def test_a_target_that_has_never_deployed_says_so_rather_than_borrowing_another_row():
    out = infra(FakeConn(
        hosts=[HOST],
        targets=[tgt(tid="deptgt_1"), tgt(app="Shorty", tid="deptgt_2")],
        deploys=[{"deployment_target_id": "deptgt_1", "status": "succeeded",
                  "commit_ref": "abc", "finished_at": None, "failure_reason": None}],
    ), "proj")
    by = {t["app_name"]: t for t in out["targets"]}
    assert by["Invoice-Chaser"]["last_deploy"]["status"] == "succeeded"
    assert by["Shorty"]["last_deploy"] is None


def test_a_failed_deploy_carries_its_reason():
    out = infra(FakeConn(
        hosts=[HOST], targets=[tgt()],
        deploys=[{"deployment_target_id": "deptgt_1", "status": "failed", "commit_ref": None,
                  "finished_at": None, "failure_reason": "port 3014 already bound"}],
    ), "proj")
    assert "already bound" in out["targets"][0]["last_deploy"]["failure_reason"]
