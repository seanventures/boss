"""Registering an agent, and giving it a place in the org.

BOSS had eight role endpoints and every one of them read. Roles arrived only by installing a
spec pack, so an agent that began doing work outside one could never be registered at all --
and an unregistered role has no trust score, no autonomy level and no violation tracking. The
console had been reporting exactly that, on five agents that between them run most of the PRD
pipeline, with no way for anyone to act on it.
"""

from datetime import datetime, timezone

import pytest

from boss_engine.services.specpack import SpecPackService

SVC = SpecPackService(None)


class FakeConn:
    """Minimal stand-in: remembers rows keyed by (project, role_key)."""

    def __init__(self):
        self.rows = {}
        self.statements = []

    def execute(self, statement, params=None):
        raw = " ".join(str(statement).split())
        p = dict(params or {})
        self.statements.append((raw, p))
        outer = self
        low = raw.lower()

        if low.startswith("select id from roles"):
            hit = outer.rows.get((p["p"], p["k"]))
            rows = [{"id": hit["id"]}] if hit else []
        elif low.startswith("insert into roles"):
            outer.rows[(p["p"], p["k"])] = {
                "id": p["id"], "project_id": p["p"], "role_key": p["k"],
                "display_name": p["name"], "autonomy_level": p["autonomy"],
                "reports_to": p["reports_to"], "spec": {}, "trust_score": 50,
                # A real row hands back a datetime, and the service calls .isoformat() on it. A string
                # here passes the insert and blows up on the read-back — the fake has to match
                # the column type, not just the shape.
                "created_at": datetime(2026, 8, 28, tzinfo=timezone.utc),
            }
            rows = []
        elif low.startswith("update roles"):
            row = outer.rows[(p["p"], p["k"])]
            if p.get("name"):
                row["display_name"] = p["name"]
            row["autonomy_level"] = p["autonomy"]
            row["reports_to"] = p["reports_to"]
            rows = []
        else:                                    # the get_role_for_project read-back
            key = p.get("role_key") or p.get("k")
            proj = p.get("project_id") or p.get("p")
            hit = outer.rows.get((proj, key))
            rows = [hit] if hit else []

        class _R:
            def mappings(self):
                return self

            def all(self):
                return rows

            def first(self):
                return rows[0] if rows else None

        return _R()


@pytest.fixture()
def conn():
    return FakeConn()





def test_an_agent_doing_work_can_be_put_on_the_books(conn):
    role = SVC.register_role(conn, "proj", "product_manager")
    assert role["role_key"] == "product_manager"
    # A name is derived rather than left blank: "product_manager" is an id, not a label.
    assert role["display_name"] == "Product Manager"


def test_registering_twice_updates_rather_than_duplicating(conn):
    SVC.register_role(conn, "proj", "qa", display_name="QA")
    SVC.register_role(conn, "proj", "qa", display_name="QA Lead", autonomy_level=2)
    inserts = [s for s, _ in conn.statements if s.lower().startswith("insert into roles")]
    assert len(inserts) == 1, "a second registration must not create a second row"
    assert conn.rows[("proj", "qa")]["display_name"] == "QA Lead"
    assert conn.rows[("proj", "qa")]["autonomy_level"] == 2


def test_a_role_can_be_given_a_manager():
    c = FakeConn()
    SVC.register_role(c, "proj", "vp_product", reports_to="product_manager")
    assert c.rows[("proj", "vp_product")]["reports_to"] == "product_manager"


def test_a_manager_need_not_exist_yet():
    """The org is discovered from observed handoffs, so a manager is often registered after the
    role reporting to them. Requiring order would make the console's register button fail for
    reasons the operator cannot see."""
    c = FakeConn()
    SVC.register_role(c, "proj", "ux_engineer", reports_to="nobody_yet")
    assert c.rows[("proj", "ux_engineer")]["reports_to"] == "nobody_yet"


def test_a_role_cannot_report_to_itself():
    """A cycle of one. It would make the org chart pick an arbitrary root and look correct."""
    c = FakeConn()
    with pytest.raises(ValueError, match="cannot report to itself"):
        SVC.register_role(c, "proj", "cto", reports_to="cto")


def test_an_empty_key_is_refused():
    c = FakeConn()
    with pytest.raises(ValueError, match="role_key is required"):
        SVC.register_role(c, "proj", "   ")


def test_a_root_has_no_manager():
    c = FakeConn()
    SVC.register_role(c, "proj", "ceo")
    assert c.rows[("proj", "ceo")]["reports_to"] is None


def test_a_role_name_boss_used_to_write_resolves_to_the_registered_one():
    """Months of llm_calls carry role='architect' and role='reviewer'. Rewriting the cost ledger
    to fix a display problem is a far worse trade than a lookup, so history is folded in."""
    from boss_engine.services.llm_telemetry import canonical_role

    assert canonical_role("architect") == "chief_architect"
    assert canonical_role("reviewer") == "adversarial_reviewer"
    # Everything else passes through untouched, normalised.
    assert canonical_role("software_engineer") == "software_engineer"
    assert canonical_role("  QA ") == "qa"
    assert canonical_role(None) is None


def test_new_calls_are_attributed_to_the_registered_names():
    """The map itself is corrected, so this stops accruing more history to fold in."""
    from boss_engine.services.llm_telemetry import STAGE_ROLES, role_for_stage

    assert role_for_stage("plan") == "chief_architect"
    assert role_for_stage("review_code") == "adversarial_reviewer"
    assert "architect" not in set(STAGE_ROLES.values())
    assert "reviewer" not in set(STAGE_ROLES.values())
