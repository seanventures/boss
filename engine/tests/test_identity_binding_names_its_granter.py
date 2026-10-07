"""A binding says WHO granted it in words (round 3: the group page showed "granted by usr_…").

bindings._present carries granted_by_name from the granter's principal (one LEFT JOIN in _SELECT);
the console shows it and never the id.
"""
from boss_engine.identity import bindings


def test_the_presented_binding_carries_the_granters_name():
    row = {"id": "rb_1", "subject_type": "group", "subject_id": "grp_1", "group_name": "Builders",
           "group_source": "manual", "role_id": "rol_1", "role_key": "operator", "scope_type": "org",
           "scope_id": None, "granted_by": "usr_ann", "granted_by_name": "Ann Admin",
           "granted_at": None, "expires_at": None, "reason": None}
    out = bindings._present(row)
    assert out["granted_by_name"] == "Ann Admin" and out["granted_by"] == "usr_ann"


def test_every_listing_joins_the_granter():
    assert "gp.display_name AS granted_by_name" in bindings._SELECT
    assert "LEFT JOIN identity_principals gp ON gp.id = b.granted_by" in bindings._SELECT
