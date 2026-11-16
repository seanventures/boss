"""THE ACCEPTANCE SESSION: a throwaway clone, every command recorded, re-checks replayed first
(AA-E, BOSSAPP-1219, epic BOSSAPP-1213).

  - Assay works in a CLONE of the PR head (never the build's workdir) with no remote: nothing it
    does can be pushed, and the clone is deleted at close.
  - Every command it runs is RECORDED by BOSS (id, command, exit code, trimmed output, duration);
    refused ones too, with why. Only loopback (or the run's own target) can be reached.
  - A RE-CHECK REPLAYS the recorded probes first with NO MODEL CALL: unchanged items whose probe
    passes are carried as MET; a changed file sends its item back to the agent.
  - The model is never asked inside an open database transaction.
"""
from __future__ import annotations

import http.server
import subprocess
import threading
from pathlib import Path

import pytest

from boss_engine.config import get_settings
from boss_engine.services.acceptance import contract as ac
from boss_engine.services.acceptance import probes as ap
from boss_engine.services.acceptance import session as asn
from boss_engine.services.acceptance import verdict as av

from ._acceptance_fixtures import load_northgate_v1


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A small repository: a Makefile whose `check` target prints and a second commit later."""
    src = tmp_path / "origin"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@example.invalid")
    _git(src, "config", "user.name", "t")
    (src / "Makefile").write_text("check:\n\t@echo gateway-ok\nfail:\n\t@echo broken; exit 3\n")
    (src / "app.py").write_text("print('serving')\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "one")
    return src


@pytest.fixture()
def settings():
    return get_settings()


class Scripted:
    """A deterministic engine: answers the plan turn, then the judge turn; counts calls."""

    def __init__(self, plan: str, judge: str):
        self.replies = [plan, judge]
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> dict:
        self.prompts.append(prompt)
        return {"summary": self.replies[len(self.prompts) - 1], "cost_usd_micros": 1000,
                "model": "scripted"}


def _never(prompt: str) -> dict:
    raise AssertionError("the model was asked on a replay")


ITEMS = [{"id": "AC-1", "n": 1, "source": "ticket", "text": "`make check` reports the gateway ok"}]


# ── the clone ───────────────────────────────────────────────────────────────────────────────

def test_the_sandbox_is_a_throwaway_clone_of_the_head_with_no_remote(repo, settings):
    head = _git(repo, "rev-parse", "HEAD")
    with asn.open_sandbox(settings, run_id="crun_t1", source=repo, head_sha=head) as sbx:
        assert sbx.head_sha == head and sbx.root != repo
        assert _git(sbx.root, "remote") == ""              # nothing to push to
        (sbx.root / "app.py").write_text("edited in the sandbox\n")   # writes are allowed
        scratch = sbx.scratch
    assert not scratch.exists()                             # nothing is kept
    assert (repo / "app.py").read_text() == "print('serving')\n"


def test_every_command_is_recorded_with_exit_output_and_duration(repo, settings):
    with asn.open_sandbox(settings, run_id="crun_t2", source=repo) as sbx:
        ok = sbx.exec("make check")
        bad = sbx.exec("make fail")
        shell = sbx.exec("make check && make fail")
        push = sbx.exec("git push origin main")
        net = sbx.exec("python3 -c \"import urllib.request; urllib.request.urlopen("
                       "'https://example.com')\"")
        cat = sbx.exec("cat /etc/passwd")
        rec = sbx.record.to_list()
    assert [c["id"] for c in rec] == ["E1", "E2", "E3", "E4", "E5", "E6"]
    assert ok.exit == 0 and "gateway-ok" in ok.stdout and ok.duration_ms >= 0
    assert ok.output_sha256 and ok.argv == ["make", "check"]
    assert bad.exit != 0 and "broken" in bad.stdout
    assert shell.refused and "shell" in shell.refused
    assert push.refused and "push" in push.refused
    assert net.refused and "example.com" in net.refused
    assert cat.refused and "allowlisted" in cat.refused      # the sandbox's own allow-list
    assert all("exit" in c or "refused" in c for c in rec)


def test_output_is_trimmed_with_the_whole_outputs_digest(repo, settings):
    with asn.open_sandbox(settings, run_id="crun_t3", source=repo) as sbx:
        big = sbx.exec("python3 -c \"print('x' * 50000)\"")
    assert big.exit == 0 and len(big.stdout) < 20000 and "trimmed" in big.stdout
    assert len(big.output_sha256) == 64


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        authed = self.headers.get("Authorization") == "Bearer good"
        self.send_response(200 if authed else 401)
        self.end_headers()
        self.wfile.write(b"ok" if authed else b'{"error":"missing token"}')

    def log_message(self, *a):  # noqa: D102
        pass


@pytest.fixture()
def server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_requests_reach_loopback_only_and_are_recorded_with_their_status(repo, settings, server):
    with asn.open_sandbox(settings, run_id="crun_t4", source=repo) as sbx:
        no = sbx.http("GET", f"{server}/payments")
        yes = sbx.http("GET", f"{server}/payments", headers={"Authorization": "Bearer good"})
        out = sbx.http("GET", "http://203.0.113.9/payments")
    assert no.status == 401 and "missing token" in no.stdout and no.exit == 0
    assert yes.status == 200
    assert out.refused and out.status is None
    with asn.open_sandbox(settings, run_id="crun_t5", source=repo, egress=asn.LOOPBACK_TARGET,
                          target_url="https://staging.example.invalid/") as sbx:
        assert sbx.refusal("python3 -c \"'https://staging.example.invalid/x'\"") is None
        assert sbx.refusal("python3 -c \"'https://other.example.invalid/x'\"")


# ── the session: record, judge, replay ──────────────────────────────────────────────────────

def _round_one(repo, settings):
    engine = Scripted(plan="AC-1 RUN make check\nRUN: make check",
                      judge='AC-1: MET — evidence: E1 (exit 0) "gateway-ok"\nACCEPTANCE: MET')
    with asn.open_sandbox(settings, run_id="crun_r1", source=repo) as sbx:
        res = asn.run_session(sbx, items=ITEMS, ask=engine, title="Gateway")
        rec = av.judge(ITEMS, res.reply, {}, root=sbx.root, exec_log=res.record,
                       replay=res.carried)
        probes = ap.from_record(rec, res.record, check_id="acc_round1")
        head = sbx.head_sha
    return engine, res, rec, probes, head


def test_round_one_runs_what_the_agent_planned_and_its_met_is_verified(repo, settings):
    engine, res, rec, probes, _ = _round_one(repo, settings)
    assert res.model_calls == 2 and len(engine.prompts) == 2
    assert "[E1] exit 0" in engine.prompts[1] and "gateway-ok" in engine.prompts[1]
    assert rec["verdict"] == "met" and rec["items"][0]["evidence_refs"][0]["verified"]
    assert probes == [{"item": "AC-1", "kind": "exec", "setup": [],
                       "action": {"cmd": "make check"},
                       "expect": {"exit": 0, "stdout_contains": "gateway-ok"}, "files": [],
                       "source": {"check_id": "acc_round1", "command_id": "E1"}}]


def test_a_recheck_replays_recorded_commands_first_with_no_model_call(repo, settings):
    _, _, _, probes, head = _round_one(repo, settings)
    with asn.open_sandbox(settings, run_id="crun_r2", source=repo) as sbx:
        res = asn.run_session(sbx, items=ITEMS, ask=_never, title="Gateway",
                              prior={"probes": probes, "head_sha": head})
        rec = av.judge(ITEMS, res.reply, {}, root=sbx.root, exec_log=res.record,
                       replay=res.carried)
        commands = res.record.to_list()
    assert res.model_calls == 0 and set(res.carried) == {"AC-1"}
    assert commands[0]["replay"] is True and commands[0]["cmd"] == "make check"
    assert rec["verdict"] == "met" and rec["replayed"] == [1]


def test_a_changed_file_or_a_failing_probe_sends_the_item_back_to_the_agent(repo, settings):
    _, _, _, probes, head = _round_one(repo, settings)
    (repo / "Makefile").write_text("check:\n\t@echo gateway-down\n")
    _git(repo, "commit", "-qam", "two")
    engine = Scripted(plan="RUN: make check",
                      judge='AC-1: MISSING — E2 (exit 0) "gateway-down"\nACCEPTANCE: NOT MET')
    with asn.open_sandbox(settings, run_id="crun_r3", source=repo) as sbx:
        res = asn.run_session(sbx, items=ITEMS, ask=engine, title="Gateway",
                              prior={"probes": [{**probes[0], "files": ["Makefile"]}],
                                     "head_sha": head})
        rec = av.judge(ITEMS, res.reply, {}, root=sbx.root, exec_log=res.record,
                       replay=res.carried)
    assert res.replayed["AC-1"]["result"] == "fail" and res.replayed["AC-1"]["changed"]
    assert res.carried == {} and res.model_calls == 2
    assert rec["verdict"] == "not_met" and av.diff({"items": [{"id": "AC-1",
                                                               "status": "met"}]},
                                                   rec)["regressed"] == ["AC-1"]


def test_an_open_rows_verify_with_probe_that_passes_proves_it_fixed(repo, settings):
    rows = [{"number": 7, "title": "check target", "verify_with": [
        {"kind": "exec", "action": {"cmd": "make check"},
         "expect": {"exit": 0, "stdout_contains": "gateway-ok"}}]}]
    with asn.open_sandbox(settings, run_id="crun_r4", source=repo) as sbx:
        res = asn.run_session(sbx, items=[], ask=_never, open_rows=rows)
    assert res.finding_results == [{"finding": "F-7", "result": "pass", "command_ids": ["E1"]}]
    assert res.model_calls == 0


def test_the_model_is_never_asked_inside_an_open_transaction(repo, settings, monkeypatch):
    from boss_engine.services.acceptance import contract

    monkeypatch.setattr(contract, "_transaction_open", lambda: True)
    engine = Scripted(plan="RUN: make check", judge="ACCEPTANCE: MET")
    with asn.open_sandbox(settings, run_id="crun_r5", source=repo) as sbx:
        monkeypatch.setenv(ac.STRICT_GUARD_ENV, "1")
        with pytest.raises(asn.ModelCallInTransaction):
            asn.run_session(sbx, items=ITEMS, ask=engine)
        monkeypatch.setenv(ac.STRICT_GUARD_ENV, "0")
        res = asn.run_session(sbx, items=ITEMS, ask=engine)
    assert engine.prompts == [] and res.stop and res.model_calls == 0


def test_planned_commands_read_run_and_http_lines_in_order():
    got = asn.planned_commands('PLAN...\nRUN: make build\nHTTP: GET http://127.0.0.1:8/x '
                               'headers {"Authorization": "Bearer t"}\n- RUN: `make test`\n')
    assert got == [{"kind": "exec", "cmd": "make build"},
                   {"kind": "http", "method": "GET", "url": "http://127.0.0.1:8/x",
                    "headers": {"Authorization": "Bearer t"}, "body": None},
                   {"kind": "exec", "cmd": "make test"}]


# ── Northgate v1: exec evidence on the real delivered tree ──────────────────────────────────

def test_northgate_run_staging_is_judged_on_what_the_command_really_printed(settings):
    """On 92f7785, `make run-staging` is docker compose, not the documented Go demo. A MET
    that misquotes the record is not judged; the MISSING stands on the command it cites."""
    ng = load_northgate_v1()
    bundle = ng.root / ng.expected["tree"]["bundle"]
    items = [{"id": "AC-15", "n": 15, "source": "ticket",
              "text": "make run-staging boots the Go gateway against staging config"},
             {"id": "AC-1", "n": 1, "source": "brief", "text": "written in Go"}]
    engine = Scripted(
        plan="RUN: make -n run-staging",
        judge=('AC-15: MISSING — E1 (exit 0) "docker compose" — run-staging is compose\n'
               'AC-1: MET — E1 (exit 0) "go build"\nACCEPTANCE: NOT MET'))
    with asn.open_sandbox(settings, run_id="crun_ng", source=bundle,
                          branch=ng.expected["tree"]["branch"]) as sbx:
        res = asn.run_session(sbx, items=items, ask=engine, title="Northgate")
        rec = av.judge(items, res.reply, {}, root=sbx.root, exec_log=res.record)
    cmd = res.record.get("E1")
    assert cmd.exit == 0 and "docker compose" in cmd.stdout
    by = {i["id"]: i for i in rec["items"]}
    assert by["AC-15"]["status"] == "missing"
    assert by["AC-1"]["status"] == "unjudged" and by["AC-1"]["unverified"]   # misquoted
    assert rec["verdict"] == "not_met"
