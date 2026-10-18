"""NOTHING LEAVES WITHOUT A PREVIEW AND A PERSON'S TICK, AND WHAT LEAVES IS WHAT WAS SHOWN (P0;
BOSSAPP-571 R9, spec §7.1).

  * `BOSS_TELEMETRY=off` / `DO_NOT_TRACK=1`: zero outbound connections — no dedupe search, no
    send — and the only way out is the patch the person exports themselves.
  * Send refuses without the tick, refuses a blocked preview, refuses a preview already sent or
    replaced by an edit (an edit makes new bytes and clears the tick).
  * The bytes sent are the bytes previewed: the stored payload's sha256 is the one shown.
"""
from __future__ import annotations

import hashlib
import json
import socket

import pytest

from boss_engine.services import self_fix_upstream as up

from ._self_fix_upstream_support import settings as _settings
from ._self_fix_upstream_support import FakeGitHub, client_for, insert_verified
from ._self_fix_support import clean_project
from .test_workflow_capability_runner import eng, project  # noqa: F401


@pytest.fixture()
def world(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.delenv("BOSS_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    fake = FakeGitHub(fork="pat-example/boss")
    monkeypatch.setattr(up, "CLIENT", lambda c, actor: client_for(fake))
    monkeypatch.setattr(up, "_names", lambda c, p: [])
    monkeypatch.setattr(up, "_fix_files", lambda s, sfx, b: [
        ("engine/boss_engine/services/stack_deploy.py", "def plan_stack_deploy(t):\n    return t\n")])
    sfx_id = insert_verified(eng, project)
    yield {"eng": eng, "project": project, "fake": fake, "sfx": sfx_id}
    clean_project(eng, project)


@pytest.mark.parametrize("env, value", [("BOSS_TELEMETRY", "off"), ("DO_NOT_TRACK", "1")])
def test_telemetry_off_means_no_connection_at_all(world, monkeypatch, env, value):
    monkeypatch.setenv(env, value)

    def no_client(c, actor):
        raise AssertionError("no GitHub client may even be made with telemetry off")

    monkeypatch.setattr(up, "CLIENT", no_client)
    real_connect = socket.socket.connect
    reached: list = []

    def guard(self, address):  # every Python-level outbound connection would land here
        reached.append(address)
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guard)
    with world["eng"].begin() as c:
        got = up.preview(c, _settings(), world["sfx"], actor="usr_test")
    assert got["channel"] == "patch_export" and env in got["channel_why"]
    assert got["dedupe"]["state"] == "skipped"
    with pytest.raises(up.UpstreamRefused, match="nothing leaves"):
        with world["eng"].begin() as c:
            up.send(c, _settings(), world["sfx"], got["send_id"], actor="usr_test",
                    checked=True)
    with pytest.raises(up.UpstreamRefused, match="nothing leaves"):
        with world["eng"].begin() as c:
            up.create_fork(c, _settings(), world["sfx"], actor="usr_test")
    assert reached == [], "telemetry off: not one outbound connection"
    # The export still works: it is the person's own disk.
    with world["eng"].begin() as c:
        body, name = up.export(c, _settings(), world["sfx"], actor="usr_test")
    assert name.endswith(".tar.gz") and body[:2] == b"\x1f\x8b"


def test_send_needs_the_tick(world):
    with world["eng"].begin() as c:
        got = up.preview(c, _settings(), world["sfx"], actor="usr_test")
    with pytest.raises(up.UpstreamRefused, match="I checked this"):
        with world["eng"].begin() as c:
            up.send(c, _settings(), world["sfx"], got["send_id"], actor="usr_test",
                    checked=False)
    assert not any(m == "POST" for m, _p, _b in world["fake"].calls), \
        "the only calls before Send are the dedupe search's reads"


def test_the_bytes_sent_are_the_bytes_previewed(world):
    with world["eng"].begin() as c:
        got = up.preview(c, _settings(), world["sfx"], actor="usr_test")
    assert got["status"] == "previewed" and got["channel"] == "github_pr", got
    with world["eng"].begin() as c:
        sent = up.send(c, _settings(), world["sfx"], got["send_id"], actor="usr_test",
                       checked=True)
    assert sent["status"] == "sent" and sent["checked"] is True
    assert sent["result"]["url"].endswith("/pull/412")
    pr = next(b for m, p, b in world["fake"].calls if m == "POST" and p.endswith("/pulls"))
    assert pr["title"] == got["title"] and pr["body"].startswith(got["body"])
    assert got["evidence"] in pr["body"]
    commit = next(b for m, p, b in world["fake"].calls if m == "POST" and
                  p.endswith("/git/commits"))
    for k, v in got["trailers"].items():
        assert f"{k}: {v}" in commit["message"]
    with world["eng"].begin() as c:
        row = c.execute(__import__("sqlalchemy").text(
            "SELECT body_gz, body_sha256 FROM self_fix_upstream_sends WHERE id = :i"),
            {"i": got["send_id"]}).mappings().first()
    import gzip

    raw = gzip.decompress(bytes(row["body_gz"]))
    assert hashlib.sha256(raw).hexdigest() == row["body_sha256"] == got["sha256"]
    assert json.loads(raw)["body"] == got["body"]
    with pytest.raises(up.UpstreamRefused, match="is sent"):
        with world["eng"].begin() as c:
            up.send(c, _settings(), world["sfx"], got["send_id"], actor="usr_test",
                    checked=True)


def test_an_edit_makes_new_bytes_and_clears_the_tick(world):
    with world["eng"].begin() as c:
        first = up.preview(c, _settings(), world["sfx"], actor="usr_test")
    with world["eng"].begin() as c:
        edited = up.edit(c, _settings(), world["sfx"], first["send_id"], actor="usr_test",
                         body="My own words about this fix.")
    assert edited["send_id"] != first["send_id"] and edited["sha256"] != first["sha256"]
    assert edited["checked"] is False
    assert up.parse_machine_block(edited["body"]), "the labeler's block is kept on an edit"
    with pytest.raises(up.UpstreamRefused, match="superseded"):
        with world["eng"].begin() as c:
            up.send(c, _settings(), world["sfx"], first["send_id"], actor="usr_test",
                    checked=True)
    with world["eng"].begin() as c:
        blocked = up.edit(c, _settings(), world["sfx"], edited["send_id"], actor="usr_test",
                          evidence="it ran on 8.8.4.4")
    # Prose a person types is checked like everything else that leaves.
    assert blocked["status"] == "blocked"


def test_only_a_verified_fix_goes_upstream(world, eng):  # noqa: F811
    other = insert_verified(eng, world["project"], status="awaiting_approval")
    with pytest.raises(up.UpstreamRefused, match="only a verified"):
        with eng.begin() as c:
            up.preview(c, _settings(), other, actor="usr_test")
