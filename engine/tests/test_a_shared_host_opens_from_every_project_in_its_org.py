"""A verified host is usable by every project in its organisation, so its id is confined to the
org, not to the project that added it. Another org's host is still refused."""
from __future__ import annotations

import pytest

from boss_engine.identity import object_rules
from boss_engine.identity.object_rules import Confine, Confined, _confine_one

HOSTS = {"dh_shared": ("org_a", "proj_added_it"), "dh_other_org": ("org_b", "proj_b")}
TARGETS = {"deptgt_1": ("org_a", "proj_added_it")}


@pytest.fixture(autouse=True)
def _loader(monkeypatch):
    def load(kind, _db, object_id):
        return (HOSTS if kind == "deployment_host" else TARGETS).get(object_id)
    monkeypatch.setattr(object_rules, "load_object", load)


def test_a_host_added_in_one_project_opens_from_another_in_the_same_org():
    _confine_one(Confine("path", "host_id", ("deployment_host",)), "dh_shared",
                 org_id="org_a", project_id="proj_other", db=None)


def test_a_host_from_another_org_is_still_refused():
    with pytest.raises(Confined):
        _confine_one(Confine("path", "host_id", ("deployment_host",)), "dh_other_org",
                     org_id="org_a", project_id="proj_other", db=None)


def test_a_target_stays_confined_to_its_own_project():
    with pytest.raises(Confined):
        _confine_one(Confine("path", "target_id", ("deployment_target",)), "deptgt_1",
                     org_id="org_a", project_id="proj_other", db=None)
