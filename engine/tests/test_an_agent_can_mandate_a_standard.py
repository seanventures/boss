"""A role could conclude something and change nothing.

The Security agent can sit in every panel and conclude that a port must be checked for public
exposure before it is assigned to an app. It can say so clearly, and the conclusion lands in one
meeting's minutes, which the next build never reads. Standards were Python constants — only the
platform could add one — so what the workforce decided was, operationally, a suggestion.

Two things had to become true for a role to mandate anything:

  1. A standard has to be DATA, so an agent can write one, scoped to this project or to all of
     them, and have the coding pipeline pick it up on the next build.
  2. A role has to be able to PERFORM the check it is mandating, or the mandate is a request for
     someone else to do work — which is how BOSS ended up diagnosing the same firewall four
     times without touching it.

The rules are carried into every pipeline stage by the same shared preamble as the platform's
own standards, so they get identical force, and each keeps who mandated it and why: a rule with
no author cannot be argued with, and an agent that knows the reason applies it to the cases the
text did not enumerate.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from boss_engine.services.platform_standards import (
    _MANDATED,
    load_mandated_standards,
    platform_standards,
)
from boss_engine.tools.builtin import (
    _DISCUSS_OPERATIONS_TOOLS,
    build_discuss_read_registry,
    build_default_registry,
)

PORT_RULE = {
    "title": "Check port exposure before assigning",
    "body": "Before binding an app to a port, confirm nothing else answers on it and that it "
            "is not reachable from the public internet.",
    "scope": "service",
    "mandated_by_role": "Security",
    "rationale": "an app silently sharing a port with something public is a breach nobody sees",
}


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        row = self.first()
        return None if row is None else list(row.values())[0]


class FakeConn:
    """Enough of a connection to answer the two queries these tools make."""

    def __init__(self, rows=(), targets=()):
        self.rows = list(rows)
        self.targets = list(targets)
        self.writes: list[tuple[str, dict]] = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        params = params or {}
        if sql.upper().startswith(("INSERT", "UPDATE")):
            self.writes.append((sql, params))
            return _Rows([])
        if "FROM deployment_targets" in sql:
            return _Rows(self.targets)
        if "SELECT id FROM engineering_standards" in sql:
            return _Rows([])
        return _Rows(self.rows)


class FakeCtx:
    def __init__(self, conn):
        self.conn = conn
        self.settings = object()

    @contextmanager
    def begin(self):
        yield self.conn


@pytest.fixture
def tools():
    return {spec.name: spec for spec in build_default_registry().specs()}


@pytest.fixture(autouse=True)
def _clean_snapshot():
    token = _MANDATED.set(())
    yield
    _MANDATED.reset(token)


class TestAMandatedRuleReachesTheBuilder:
    """The whole point: a role writes a rule and the next build is told about it."""

    def test_it_is_absent_until_somebody_mandates_it(self):
        assert "Check port exposure" not in platform_standards("an http api")

    def test_once_mandated_it_is_in_the_prompt_every_stage_shares(self):
        load_mandated_standards(FakeConn([PORT_RULE]), "boss-dogfood")
        assert "Check port exposure before assigning" in platform_standards("an http api")

    def test_the_role_that_mandated_it_is_named(self):
        """An anonymous rule cannot be questioned, and the first question about any standard
        is who decided it."""
        load_mandated_standards(FakeConn([PORT_RULE]), "boss-dogfood")
        assert "Security" in platform_standards("an http api")

    def test_and_so_is_the_reason(self):
        """An agent that understands the constraint applies it to cases the text did not
        enumerate; one that only has the text follows it literally and misses them."""
        load_mandated_standards(FakeConn([PORT_RULE]), "boss-dogfood")
        assert "breach nobody sees" in platform_standards("an http api")

    def test_the_platform_standards_are_still_there_too(self):
        """Mandated rules are additional, not a replacement — the observability standard did
        not stop applying because Security added one."""
        load_mandated_standards(FakeConn([PORT_RULE]), "boss-dogfood")
        block = platform_standards("an http api")
        assert "/metrics" in block and "Check port exposure" in block

    def test_a_library_is_spared_the_operational_ones(self):
        """A CLI told to expose /metrics produces cargo-culted dead code."""
        load_mandated_standards(FakeConn([PORT_RULE]), "boss-dogfood")
        assert platform_standards("a parsing library") == ""

    def test_but_an_all_scoped_rule_binds_even_a_library(self):
        rule = {**PORT_RULE, "scope": "all", "title": "Licence header on every file"}
        load_mandated_standards(FakeConn([rule]), "boss-dogfood")
        assert "Licence header" in platform_standards("a parsing library")

    def test_a_missing_table_does_not_break_a_build(self):
        """Standards are a prompt improvement. A build must not fail because one cannot be
        read — the pipeline predates the table and must survive rollback to a version without
        it."""
        class Exploding(FakeConn):
            def execute(self, statement, params=None):
                raise RuntimeError('relation "engineering_standards" does not exist')

        assert load_mandated_standards(Exploding(), "boss-dogfood") == 0
        assert platform_standards("an http api")  # the platform's own still apply


class TestScopingToOneProjectOrAllOfThem:
    def test_all_projects_is_stored_as_a_null_project(self, tools):
        """The user's case: a rule Security wants applied everywhere, not to one app."""
        conn = FakeConn()
        tools["mandate_standard"].handler(
            FakeCtx(conn), project_id="boss-dogfood", actor="Security",
            args={"key": "port_exposure_check", "title": PORT_RULE["title"],
                  "body": PORT_RULE["body"], "rationale": PORT_RULE["rationale"],
                  "all_projects": True})
        _sql, params = conn.writes[0]
        assert params["p"] is None

    def test_a_one_off_binds_only_the_project_it_was_asked_about(self, tools):
        conn = FakeConn()
        out = tools["mandate_standard"].handler(
            FakeCtx(conn), project_id="boss-dogfood", actor="Security",
            args={"key": "port_exposure_check", "title": PORT_RULE["title"],
                  "body": PORT_RULE["body"], "rationale": PORT_RULE["rationale"]})
        assert conn.writes[0][1]["p"] == "boss-dogfood"
        assert out["applies_to"] == "boss-dogfood"

    def test_the_role_is_recorded_not_the_session_identity(self, tools):
        """Every chat turn runs as the same actor. Recording that would attribute every rule
        the workforce ever makes to one anonymous account, and 'who decided this' is the first
        question anybody asks about a rule they have to follow. Observed live: Security's port
        rule came back authored by 'orchestrator'."""
        conn = FakeConn()
        tools["mandate_standard"].handler(
            FakeCtx(conn), project_id="boss-dogfood", actor="orchestrator",
            args={"key": "k", "title": "t", "body": "b", "rationale": "r",
                  "role": "Security"})
        assert conn.writes[0][1]["r"] == "Security"

    def test_without_a_role_it_falls_back_to_the_actor(self, tools):
        conn = FakeConn()
        tools["mandate_standard"].handler(
            FakeCtx(conn), project_id="boss-dogfood", actor="ceo",
            args={"key": "k", "title": "t", "body": "b", "rationale": "r"})
        assert conn.writes[0][1]["r"] == "ceo"

    def test_it_does_not_let_the_agent_believe_it_gated_the_deploy(self, tools):
        """The first live mandate came back described to the operator as "will block
        deployments that expose a port publicly". It will not: a standard is injected into
        build instructions, and nothing checks it at release. A rule that promises enforcement
        nobody performs is worse than none, because everyone downstream believes it is
        covered."""
        out = tools["mandate_standard"].handler(
            FakeCtx(FakeConn()), project_id="boss-dogfood", actor="ceo",
            args={"key": "k", "title": "t", "body": "b", "rationale": "r"})
        assert "does NOT gate deploys" in out["note"]
        assert "not a deploy-time gate" in tools["mandate_standard"].description.lower() \
            or "NOT a deploy-time gate" in tools["mandate_standard"].description

    def test_a_rule_with_no_stated_reason_is_refused(self, tools):
        """Followed narrowly and ignored at the edges is what happens to rules with no why —
        and nobody can later judge whether the rule still applies."""
        out = tools["mandate_standard"].handler(
            FakeCtx(FakeConn()), project_id="boss-dogfood", actor="Security",
            args={"key": "k", "title": "t", "body": "b"})
        assert not out["ok"] and "rationale" in out["error"]

    def test_re_mandating_the_same_key_updates_rather_than_duplicates(self, tools):
        """Two rules saying the same thing teach agents that standards are noise."""
        conn = FakeConn()
        conn.execute = lambda statement, params=None: (  # type: ignore[method-assign]
            _Rows([{"id": "std_existing"}])
            if "SELECT id FROM engineering_standards" in str(statement)
            else (conn.writes.append((" ".join(str(statement).split()), params or {}))
                  or _Rows([])))
        tools["mandate_standard"].handler(
            FakeCtx(conn), project_id="boss-dogfood", actor="Security",
            args={"key": "port_exposure_check", "title": "t", "body": "b", "rationale": "r"})
        assert conn.writes and conn.writes[0][0].upper().startswith("UPDATE")


class TestSecurityCanActuallyRunTheCheckItMandates:
    """Mandating a check nobody can perform is a request, not a control."""

    def test_the_port_check_exists_as_a_tool(self, tools):
        assert "check_port_exposure" in tools

    def test_it_is_a_read_because_probing_changes_nothing(self, tools):
        assert tools["check_port_exposure"].kind == "read"

    def test_a_closed_port_is_reported_as_closed(self, tools):
        """Port 1 on localhost: nothing has ever listened there."""
        out = tools["check_port_exposure"].handler(
            FakeCtx(FakeConn()), project_id="p", actor="Security",
            args={"host": "127.0.0.1", "ports": [1]})
        assert out["findings"][0]["reachable_from_boss"] is False

    def test_it_does_not_claim_public_from_its_own_reachability(self, tools):
        """BOSS reaching a port means open TO BOSS. On a tailnet that is not the same as open
        to the internet, and stating it as 'public' would be the fabrication this whole area
        exists to stop."""
        out = tools["check_port_exposure"].handler(
            FakeCtx(FakeConn()), project_id="p", actor="Security",
            args={"host": "127.0.0.1", "ports": [1]})
        assert "PUBLIC" in out["findings"][0]["means"] or "BOSS" in out["findings"][0]["means"]

    def test_it_says_which_app_already_owns_a_port(self, tools):
        """'Is this port free?' is the question being asked when a port is assigned."""
        conn = FakeConn(targets=[{"app_name": "HookWatch",
                                  "domains": ["192.0.2.202:3020"]}])
        out = tools["check_port_exposure"].handler(
            FakeCtx(conn), project_id="p", actor="Security",
            args={"host": "192.0.2.202", "ports": [3020, 3021]})
        by_port = {f["port"]: f["already_assigned_to"] for f in out["findings"]}
        assert by_port[3020] == "HookWatch" and by_port[3021] is None

    def test_it_refuses_without_a_host_or_a_port(self, tools):
        out = tools["check_port_exposure"].handler(
            FakeCtx(FakeConn()), project_id="p", actor="Security", args={"host": "", "ports": []})
        assert not out["ok"]


class TestTheseAreReachableFromChat:
    """A capability the agent cannot call in conversation is a capability the operator cannot
    ask for — which was the state of every fix BOSS could describe and not perform."""

    def test_an_agent_can_mandate_in_conversation(self):
        assert "mandate_standard" in build_discuss_read_registry().names()

    def test_it_is_admitted_deliberately_not_by_accident(self):
        assert "mandate_standard" in _DISCUSS_OPERATIONS_TOOLS

    def test_reading_the_current_rules_is_reachable_too(self):
        """Proposing a rule without reading the existing ones is how you get three rules that
        contradict each other."""
        assert "list_standards" in build_discuss_read_registry().names()

    def test_the_check_is_reachable(self):
        assert "check_port_exposure" in build_discuss_read_registry().names()


class TestTheBuildActuallyLoadsThem:
    def test_the_pipeline_loads_mandated_standards_before_it_builds(self):
        """The table can be full and the prompt still empty if nothing reads it — which is the
        failure this whole feature is correcting, one layer down."""
        import inspect

        from boss_engine.services import governed_code_task_service as svc

        body = inspect.getsource(svc.run_governed_code_task)
        assert "load_mandated_standards" in body
