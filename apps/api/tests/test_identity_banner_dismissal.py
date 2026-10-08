"""Console banners can be closed, each by its policy (identity/banner_policy.py).

  * × closes a dismissible banner for that person, server-side: every device, every reload.
  * A NEW event of the same kind (another new device) is a new id and shows again.
  * A persistent banner (MFA required, none enrolled) cannot be closed: 409, still shown.
  * An acknowledge banner (an Owner recovered from the host shell) is closed with "Got it", and
    that is audited as banner.acknowledged.
  * One person's dismissal is theirs alone; nobody can close a banner they are not shown.
  * Every banner kind has a rule; a legacy countdown closes for 24 h and never in its last 3 days.
"""
from __future__ import annotations

import typing
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    csrf,
    fresh_client,
    make_user,
    promote_fresh_mfa,
    set_policy,
    sign_in,
    world,
)
from boss_engine.identity import authn, banner_policy
from boss_engine.identity.contracts import MeBanner


def _event(engine, pid, event_type, *, ago=timedelta(0)) -> int:
    with engine.begin() as db:
        org = db.execute(text("SELECT org_id FROM identity_principals WHERE id = :p"), {"p": pid}).scalar()
        seq = int(db.execute(text("SELECT coalesce(max(seq), 0) + 1 FROM identity_security_events "
                                  "WHERE org_id = :o"), {"o": org}).scalar())
        db.execute(text("INSERT INTO identity_security_events (org_id, seq, ts, stream, event_type, outcome, "
                        "actor_principal_id, session_ref, ip, prev_hash, hash) VALUES (:o, :s, now() - :ago, "
                        "'security', :t, 'info', :p, 'ses_elsewhere', '203.0.113.9', :h, :h)"),
                   {"o": org, "s": seq, "t": event_type, "p": pid, "ago": ago, "h": b"\0" * 32})
    return seq


def _banners(client, kind=None):
    me = client.get("/v1/identity/me")
    assert me.status_code == 200, me.text
    return [b for b in me.json()["banners"] if kind is None or b["kind"] == kind]


def _person(engine, email, role="operator"):
    pid = make_user(engine, email, role=role)
    client = fresh_client()
    assert sign_in(client, email).status_code == 200
    return pid, client


def _dismiss(client, banner_id, headers=None):
    return client.post(f"/v1/identity/me/banners/{banner_id}/dismiss",
                       headers=headers if headers is not None else csrf(client))


def test_a_closed_new_device_notice_stays_closed_everywhere_until_the_next_new_device(world, engine):
    world.smtp = False
    pid, laptop = _person(engine, "bd1@example.test")
    seq = _event(engine, pid, "auth.new_device")
    [notice] = _banners(laptop, "new_device")
    assert notice["id"] == f"new_device.{seq}" and notice["policy"] == "dismissible"
    res = _dismiss(laptop, notice["id"])
    assert res.status_code == 200, res.text
    assert res.json() == {"id": notice["id"], "policy": "dismissible", "hidden_until": None}
    assert _banners(laptop, "new_device") == []                     # this reload
    phone = fresh_client()                                          # another device of theirs
    assert sign_in(phone, "bd1@example.test").status_code == 200
    assert [b["id"] for b in _banners(phone, "new_device")] != [notice["id"]]
    # A NEW sign-in from a new device is news again.
    again = _event(engine, pid, "auth.new_device")
    assert [b["id"] for b in _banners(laptop, "new_device")] == [f"new_device.{again}"]
    assert not world.events_of("banner.acknowledged")               # a plain dismissal is not audited


def test_a_new_device_notice_ages_out_after_48_hours(world, engine):
    world.smtp = False
    pid, client = _person(engine, "bd2@example.test")
    _event(engine, pid, "auth.new_device", ago=timedelta(hours=49))
    assert _banners(client, "new_device") == []
    _event(engine, pid, "auth.new_device", ago=timedelta(hours=47))
    assert len(_banners(client, "new_device")) == 1


def test_a_dismissal_is_one_persons_and_nobody_closes_a_banner_they_are_not_shown(world, engine):
    world.smtp = False
    a, alice = _person(engine, "bd3a@example.test")
    b, bob = _person(engine, "bd3b@example.test")
    seq_a = _event(engine, a, "auth.new_device")
    _event(engine, b, "auth.new_device")
    assert _dismiss(bob, f"new_device.{seq_a}").status_code == 404     # alice's, not bob's
    assert [x["id"] for x in _banners(alice, "new_device")] == [f"new_device.{seq_a}"]
    assert _dismiss(alice, f"new_device.{seq_a}").status_code == 200
    assert len(_banners(bob, "new_device")) == 1                       # bob still sees his
    assert _dismiss(alice, "new_device.999999").status_code == 404     # made up
    assert _dismiss(alice, "no such/thing").status_code == 404


def test_closing_a_banner_needs_the_csrf_token(world, engine):
    world.smtp = False
    pid, client = _person(engine, "bd4@example.test")
    seq = _event(engine, pid, "auth.new_device")
    res = _dismiss(client, f"new_device.{seq}", headers=HEADERS)
    assert res.status_code == 403 and res.json()["error"] == "csrf_failed"
    assert len(_banners(client, "new_device")) == 1


def test_a_persistent_banner_cannot_be_closed(world, engine):
    set_policy(engine, mfa_required_roles=["operator"])
    _pid, client = _person(engine, "bd5@example.test")
    [mfa] = _banners(client, "mfa_enrolment_required")
    assert mfa["policy"] == "persistent" and mfa["id"] == "mfa_enrolment_required"
    res = _dismiss(client, mfa["id"])
    # Refused twice over: a person who must enrol can write only enrolment (403), and the banner's
    # own policy refuses it (409; banner_policy.dismiss, pinned for countdowns below).
    assert res.status_code == 403 and res.json()["error"] == "mfa_enrolment_required", res.text
    assert len(_banners(client, "mfa_enrolment_required")) == 1
    assert _banners(client)[0]["kind"] == "mfa_enrolment_required"     # the binding one comes first


def test_an_owner_recovered_notice_is_acknowledged_per_person_and_audited(world, engine):
    owner, oc = _person(engine, "bd6o@example.test", role="owner")
    admin, ac = _person(engine, "bd6a@example.test", role="admin")
    _op, opc = _person(engine, "bd6p@example.test")
    for c in (oc, ac):
        promote_fresh_mfa(engine, c.cookies.get(authn.SESSION_COOKIE))
    seq = _event(engine, owner, "owner.recovered_by_host")
    [notice] = _banners(oc, "owner_recovered")
    assert notice == {**notice, "id": f"owner_recovered.{seq}", "policy": "acknowledge"}
    assert len(_banners(ac, "owner_recovered")) == 1                   # Admins see it too
    assert _banners(opc, "owner_recovered") == []                      # an Operator does not
    res = _dismiss(oc, notice["id"])
    assert res.status_code == 200 and res.json()["policy"] == "acknowledge"
    [ack] = world.events_of("banner.acknowledged")
    assert ack.actor_principal_id == owner and ack.target_id == notice["id"]
    assert dict(ack.details) == {"banner": "owner_recovered"}
    assert _banners(oc, "owner_recovered") == []
    assert len(_banners(ac, "owner_recovered")) == 1                   # the Admin has not, yet


# ---- the rules, as a class ----------------------------------------------------------------------------

def test_every_banner_kind_has_a_rule():
    kinds = set(typing.get_args(MeBanner.model_fields["kind"].annotation))
    assert kinds == set(banner_policy.RULES)


def test_a_legacy_countdown_closes_for_a_day_and_never_in_its_last_three_days(world, engine):
    pid = make_user(engine, "bd7@example.test")
    now = datetime.now(timezone.utc)
    far = {"kind": "legacy_password", "message": "m", "until": now + timedelta(days=20), "instance": "d1"}
    near = {**far, "until": now + timedelta(days=2), "instance": "d2"}
    assert [b["policy"] for b in banner_policy.shaped([far, near], now)] == ["persistent", "dismissible"]
    with engine.begin() as db:
        closed = banner_policy.dismiss(db, pid, "legacy_password.d1", [far], now)
        assert closed["hidden_until"] == now + timedelta(hours=24)
        assert banner_policy.visible(db, pid, [far], now) == []
        assert len(banner_policy.visible(db, pid, [far], now + timedelta(hours=25))) == 1   # back tomorrow
        try:
            banner_policy.dismiss(db, pid, "legacy_password.d2", [near], now)
            raise AssertionError("a countdown in its final 3 days closed")
        except banner_policy.NotDismissible as exc:
            assert exc.status == 409
    unclaimed = {"kind": "network_bridge", "message": "m", "until": None, "instance": None}
    assert banner_policy.shaped([unclaimed], now)[0]["policy"] == "persistent"
