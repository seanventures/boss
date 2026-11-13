"""boss-runner units (BOSSAPP-828): outbox seq/ACK, the command journal and its replay, adoption,
the token checks, and the DPoP-signed push. No docker, no database."""
from __future__ import annotations

import json
import sys
import time
import uuid

import pytest

from apps.runner.boss_runner.auth import RunnerAuthError, RunnerKey
from apps.runner.boss_runner.daemon import CommandError, Runner
from apps.runner.boss_runner.store import Store
from apps.runner.tests._support import Issuer, ReplayDb, verify_runner_call

RUN = "run_ct:crun_828"


def _runner(tmp_path, issuer, **kw):
    return Runner(tmp_path / "state", api_url="http://api.test", key_for_kid=issuer.key_for_kid,
                  interval=3600, transport=kw.pop("transport", lambda *a: (503, None)), **kw)


def _auth(issuer, runner, run_id=RUN, **kw):
    return "DPoP " + issuer.mint(run_id=run_id, jkt=runner.key.jkt, **kw)


def _start(runner, issuer, *, key="crun_828#a", argv=None, command_id=None, **body):
    return runner.command({"kind": "start", "command_id": command_id or uuid.uuid4().hex, "run_id": RUN,
                           "task_run_id": "crun_828", "session_key": key,
                           "argv": argv or [sys.executable, "-c", "print('hi')"], "cwd": "", **body},
                          _auth(issuer, runner))


def _wait_exit(runner, sid, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if runner.store.session(sid)["state"] not in ("starting", "running"):
            return runner.store.session(sid)
        time.sleep(0.05)
    raise AssertionError("session did not exit")


# -- outbox ------------------------------------------------------------------------------------

def _store_with_session(tmp_path):
    s = Store(tmp_path / "r.db")
    s.create_session(session_id="rs_1", run_id=RUN, task_run_id="crun_828", session_key="k", boot_id="b",
                     cwd="", argv=["x"], collect=[], api_base="", deadline=None)
    return s


def test_outbox_sequence_is_gapless_and_the_ack_is_cumulative(tmp_path):
    s = _store_with_session(tmp_path)
    seqs = [s.append("rs_1", "stdout", {"text": str(i)}) for i in range(5)]
    assert seqs == [1, 2, 3, 4, 5]
    assert [e["seq"] for e in s.pending("rs_1")] == [1, 2, 3, 4, 5]
    assert s.ack("rs_1", 3) == 3
    assert [e["seq"] for e in s.pending("rs_1")] == [4, 5]          # resumes after the ack
    assert s.ack("rs_1", 1) == 3                                      # never backwards
    assert s.ack("rs_1", 99) == 5                                     # never past what exists
    assert s.pending("rs_1") == []
    assert s.append("rs_1", "exited", {"exit_code": 0}) == 6          # seq keeps counting


def test_the_outbox_survives_a_reopen(tmp_path):
    s = _store_with_session(tmp_path)
    s.append("rs_1", "stdout", {"text": "a"})
    s.append("rs_1", "stdout", {"text": "b"})
    s.ack("rs_1", 1)
    s.close()
    again = Store(tmp_path / "r.db")
    assert [(e["seq"], e["data"]["text"]) for e in again.pending("rs_1")] == [(2, "b")]


# -- journal -----------------------------------------------------------------------------------

def test_a_start_delivered_twice_spawns_once(tmp_path):
    issuer = Issuer()
    r = _runner(tmp_path, issuer)
    first = _start(r, issuer, command_id="cmd_start_1")
    again = _start(r, issuer, command_id="cmd_start_1")
    assert again["session_id"] == first["session_id"] and again["duplicate"] is True
    assert len(r.store.sessions()) == 1


def test_a_steer_delivered_twice_is_applied_once(tmp_path):
    issuer = Issuer()
    r = _runner(tmp_path, issuer)
    sid = _start(r, issuer, argv=[sys.executable, "-c", "import time; time.sleep(5)"])["session_id"]
    body = {"kind": "steer", "command_id": "cmd_steer_1", "session_id": sid, "message": "use pytest"}
    r.command(body, _auth(issuer, r))
    r.command(body, _auth(issuer, r))
    steers = (r.supervisor.session_dir(sid) / "steer.jsonl").read_text().splitlines()
    assert len(steers) == 1
    assert [e["type"] for e in r.store.pending(sid)].count("steered") == 1
    r.command({"kind": "cancel", "command_id": "cmd_cancel_1", "session_id": sid}, _auth(issuer, r))
    assert _wait_exit(r, sid)["cancelled"] == 1


def test_a_reused_command_id_for_another_kind_is_refused(tmp_path):
    issuer = Issuer()
    r = _runner(tmp_path, issuer)
    sid = _start(r, issuer, command_id="cmd_same_1")["session_id"]
    with pytest.raises(CommandError) as err:
        r.command({"kind": "cancel", "command_id": "cmd_same_1", "session_id": sid}, _auth(issuer, r))
    assert err.value.code == "command_id_reused"


def test_boot_replays_the_journal_and_reports_lost_turns(tmp_path):
    issuer = Issuer()
    r = _runner(tmp_path, issuer)
    # What a runner that was SIGKILLed mid-turn leaves in its volume: a session its boot was
    # running, a cancel it had received but not applied, and a start it never got to.
    r.store.create_session(session_id="rs_prev", run_id=RUN, task_run_id="crun_828", session_key="crun_828#a",
                           boot_id="boot_previous", cwd="", argv=["x"], collect=[], api_base="", deadline=None)
    r.store.update_session("rs_prev", state="running", pid=999999, pgid=999999)
    r.store.journal_begin("cmd_pending_cancel", "cancel", {"kind": "cancel", "session_id": "rs_prev"},
                          session_id="rs_prev")
    r.store.journal_begin("cmd_pending_start", "start", {"kind": "start", "run_id": RUN})
    r.store.close()

    again = Runner(tmp_path / "state", api_url="http://api.test", key_for_kid=issuer.key_for_kid, interval=3600,
                   transport=lambda *a: (503, None))
    assert again.replayed == {"lost": 1, "replayed": 1, "failed": 1}
    row = again.store.session("rs_prev")
    assert row["state"] == "lost"
    assert again.store.journal_get("cmd_pending_cancel")["status"] == "applied"
    assert again.store.journal_get("cmd_pending_start")["status"] == "failed"
    last = again.store.pending("rs_prev")[-1]
    assert last["type"] == "exited" and last["data"]["lost"] is True
    assert again.runner_id == r.runner_id and again.boot_id != r.boot_id
    # A lost turn is never re-attached: the next start makes a new session.
    fresh = _start(again, issuer, key="crun_828#a")
    assert fresh["adopted"] is False


# -- adoption ----------------------------------------------------------------------------------

def test_a_start_for_the_same_turn_re_attaches_instead_of_spawning(tmp_path):
    issuer = Issuer()
    r = _runner(tmp_path, issuer)
    first = _start(r, issuer, argv=[sys.executable, "-c", "import time; time.sleep(5)"])
    second = _start(r, issuer)         # a NEW command id: the restarted API asking again
    assert second["adopted"] is True and second["session_id"] == first["session_id"]
    assert len(r.store.sessions()) == 1
    # Finished but not taken is still adoptable; once closed it is not.
    r.command({"kind": "cancel", "command_id": uuid.uuid4().hex, "session_id": first["session_id"]},
              _auth(issuer, r))
    _wait_exit(r, first["session_id"])
    assert _start(r, issuer)["adopted"] is True
    r.command({"kind": "close", "command_id": uuid.uuid4().hex, "session_id": first["session_id"]},
              _auth(issuer, r))
    third = _start(r, issuer)
    assert third["adopted"] is False and third["session_id"] != first["session_id"]


def test_a_different_turn_for_the_same_owner_ends_the_stale_one(tmp_path):
    issuer = Issuer()
    r = _runner(tmp_path, issuer)
    old = _start(r, issuer, key="crun_828#old", argv=[sys.executable, "-c", "import time; time.sleep(30)"])
    seat = _start(r, issuer, key="crun_828/seat_1#x", argv=[sys.executable, "-c", "import time; time.sleep(30)"])
    new = _start(r, issuer, key="crun_828#new")
    assert new["adopted"] is False
    assert _wait_exit(r, old["session_id"])["cancelled"] == 1
    assert r.store.session(seat["session_id"])["state"] == "running"      # another owner: untouched
    r.stop()


# -- auth --------------------------------------------------------------------------------------

def test_the_runner_accepts_only_tokens_bound_to_its_key_for_that_run(tmp_path):
    issuer = Issuer()
    r = _runner(tmp_path, issuer)
    body = {"kind": "start", "command_id": uuid.uuid4().hex, "run_id": RUN, "session_key": "k#1",
            "argv": [sys.executable, "-c", "pass"]}
    cases = {
        "dpop_token_required": None,
        "not_bound_to_this_runner": "DPoP " + issuer.mint(run_id=RUN, jkt=RunnerKey().jkt),
        "token_for_another_run": "DPoP " + issuer.mint(run_id="run_ct:other", jkt=r.key.jkt),
        "expired": "DPoP " + issuer.mint(run_id=RUN, jkt=r.key.jkt, iat=int(time.time()) - 900),
        "token_bad_signature": "DPoP " + Issuer().mint(run_id=RUN, jkt=r.key.jkt),
        "bad_aud": "DPoP " + issuer.mint(run_id=RUN, jkt=r.key.jkt, aud="boss:egress"),
    }
    for code, header in cases.items():
        with pytest.raises(RunnerAuthError) as err:
            r.command(dict(body), header)
        assert err.value.code == code
    assert r.store.sessions() == []


# -- uplink ------------------------------------------------------------------------------------

def test_the_push_is_dpop_signed_and_resumes_from_the_ack(tmp_path):
    issuer = Issuer()
    db = ReplayDb()
    calls: list[dict] = []
    api = {"up": False, "held": 0}

    def transport(method, url, headers, body, ctype):
        if not api["up"]:
            raise ConnectionError("api restarting")
        verify_runner_call(headers, method=method, url="https://boss.example" + url.split("http://api.test", 1)[1],
                           db=db)
        events = json.loads(body)["events"]
        calls.append({"seqs": [e["seq"] for e in events]})
        for e in events:                        # the API keeps only a contiguous run
            if e["seq"] == api["held"] + 1:
                api["held"] = e["seq"]
        return 200, {"ack": api["held"]}

    r = _runner(tmp_path, issuer, transport=transport)
    sid = _start(r, issuer, argv=[sys.executable, "-c", "print('a'); print('b')"],
                 api_base="https://boss.example")["session_id"]
    _wait_exit(r, sid)
    before = [e["seq"] for e in r.store.pending(sid)]
    assert r.uplink.push_all() == {}                       # API away: nothing lost
    assert [e["seq"] for e in r.store.pending(sid)] == before
    api["up"] = True
    acks = r.uplink.push_all()
    assert acks[sid] == before[-1] and r.store.pending(sid) == []
    assert calls[0]["seqs"] == before
    assert r.uplink.push_all()[sid] == before[-1]          # an empty push is the heartbeat
    assert calls[-1]["seqs"] == []
