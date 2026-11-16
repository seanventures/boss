"""THE ACCEPTANCE SESSION: sealed or not at all, every command recorded, re-checks replayed first
(AA-E, BOSSAPP-1219, epic BOSSAPP-1213).

  - Commands run ONLY as a uid of the SEALED sub-pool: its own uid (no BOSS HOME, keychain,
    ~/.ssh or .env), loopback-only network (entrypoint.sh seal_network), git with no system or
    global config and no credential helper. Where that fence is unavailable, `open_sandbox`
    REFUSES — it never runs a command unfenced (PR #151 review S1/S2).
  - Every command is RECORDED (id, command, exit code, trimmed output, duration, whether the
    session had changed the clone); refused ones too, with why.
  - A RE-CHECK REPLAYS the recorded probes first with NO MODEL CALL: unchanged items whose probe
    passes are carried as MET; a changed file, an unknown diff or a probe with no file sends the
    item back to the agent.
  - The model is never asked inside an open database transaction.

The session's LOGIC (record, judge, replay) is exercised below through a TEST-ONLY runner that
runs allow-listed commands as this uid in a temp clone — production has no such runner. The
ESCAPE tests run the real sealed sandbox and are skipped where no fence exists (a developer's
Mac); there, the refusal is what is tested.
"""
from __future__ import annotations

import http.server
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

import pytest

from boss_engine.config import get_settings
from boss_engine.services import sandbox_identity as si
from boss_engine.services.acceptance import contract as ac
from boss_engine.services.acceptance import probes as ap
from boss_engine.services.acceptance import session as asn
from boss_engine.services.acceptance import verdict as av

from ._acceptance_fixtures import load_northgate_v1
from .test_sandbox_network_fence import _run as run_entrypoint


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture()
def settings():
    return get_settings()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
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


class _TestRunner:
    """TEST ONLY — the session's logic without the fence: the sandbox allow-list and no-shell
    rule, run as this uid in a temp clone. Production runs every command through
    session._FencedRunner (the sealed uid) or not at all."""

    def __init__(self, settings, root: Path):
        self.settings, self.root = settings, root

    def run(self, command: str, *, timeout: int, cwd=None):
        from boss_engine.services.sandbox import (CommandResult, SandboxError,
                                                  assert_allowed_executable, looks_like_shell)

        argv = shlex.split(command)
        if looks_like_shell(command):
            raise SandboxError("shell syntax: sandbox commands run without a shell")
        assert_allowed_executable(argv[0], self.settings)
        started = time.monotonic()
        p = subprocess.run(argv, cwd=cwd or str(self.root), capture_output=True, text=True,
                           timeout=timeout,
                           env={"PATH": os.environ["PATH"], "HOME": os.environ.get("ACC_TEST_HOME") or str(self.root),
                                "PYTHONDONTWRITEBYTECODE": "1"})
        return CommandResult(command=command, argv=argv, exit_code=p.returncode,
                             duration_ms=int((time.monotonic() - started) * 1000),
                             timed_out=False, stdout=p.stdout, stderr=p.stderr)


def _sandbox(settings, source: Path, tmp: Path, *, head=None, branch=None, **kw):
    work = tmp / f"acc-{len(list(tmp.iterdir()))}"
    work.mkdir()
    root = work / "clone"
    asn._git(["clone", "--quiet", "--no-hardlinks", str(source), str(root)], work)
    if head or branch:
        asn._git(["checkout", "--quiet", "--detach", asn._commit_of(root, head or branch)], root)
    asn._cut_remotes(root)
    return asn.AcceptanceSandbox(root=root, head_sha=asn._git(["rev-parse", "HEAD"], root),
                                 runner=_TestRunner(settings, root), scratch=work, **kw)


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
FENCED = asn.fence_problem(get_settings()) is None


# ── the fence: sealed, or refused ───────────────────────────────────────────────────────────

@pytest.mark.skipif(FENCED, reason="this host has the sealed fence; the escape tests run instead")
def test_without_the_sealed_fence_the_sandbox_refuses_and_runs_nothing(repo, settings):
    """On a host with no fence (this Mac) nothing runs: no clone, no command, no escape."""
    with pytest.raises(asn.AcceptanceFenceUnavailable) as got:
        asn.open_sandbox(settings, run_id="crun_refused", source=repo)
    assert "unfenced" in str(got.value)


def test_a_fence_without_the_sealed_network_rules_is_refused(repo, settings, monkeypatch):
    from boss_engine.services import local_fenced

    monkeypatch.setattr(local_fenced, "fence_problem", lambda s: None)
    monkeypatch.setattr(si, "network_sealed", lambda: False)
    with pytest.raises(asn.AcceptanceFenceUnavailable) as got:
        asn.open_sandbox(settings, run_id="crun_unsealed", source=repo)
    assert "sealed" in str(got.value)


def test_a_sealed_lease_is_refused_rather_than_run_unisolated(monkeypatch):
    monkeypatch.setenv(si.MODE_ENV, "auto")
    monkeypatch.setattr(si, "available", lambda: False)
    with pytest.raises(si.SandboxUnavailable):
        si.acquire("acceptance:x", sealed=True, seed_logins=False)
    monkeypatch.setattr(si, "available", lambda: True)
    monkeypatch.setattr(si, "network_sealed", lambda: False)
    with pytest.raises(si.SandboxUnavailable):
        si.acquire("acceptance:y", sealed=True, seed_logins=False)


def test_sealed_uids_are_their_own_sub_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "SHIM_ROOT", str(tmp_path))
    sealed, fd1 = si._lease_uid("k1", sealed=True)
    plain, fd2 = si._lease_uid("k1")
    os.close(fd1)
    os.close(fd2)
    assert si.SEALED_BASE <= sealed < si.UID_BASE + si.UID_COUNT
    assert si.UID_BASE <= plain < si.SEALED_BASE


def test_a_sealed_identity_has_no_proxy_and_a_git_that_cannot_authenticate(tmp_path, monkeypatch):
    monkeypatch.setenv(si.EGRESS_CONNECT_ENV, "http://egress:8091")
    ident = si.SandboxIdentity(key="k", uid=si.SEALED_BASE, run_dir=tmp_path,
                               credential_free=True, sealed=True)
    env = ident.env({"PATH": "/usr/bin", "HTTPS_PROXY": "http://egress:8091",
                     "SSH_AUTH_SOCK": "/tmp/agent"})
    assert not any(k.lower().endswith("_proxy") and k.lower() != "no_proxy" for k in env)
    assert "SSH_AUTH_SOCK" not in env and env["HOME"] == str(tmp_path / "home")
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
             for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["credential.helper"] == ""
    plain = si.SandboxIdentity(key="k", uid=si.UID_BASE, run_dir=tmp_path).env({"PATH": "/bin"})
    assert plain["HTTPS_PROXY"] == "http://egress:8091"      # ordinary builds unchanged


def test_the_entrypoint_seals_the_sub_pool_to_loopback_ahead_of_the_pool(tmp_path):
    result, root, calls = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (root / ".network-sealed").is_file() and (root / ".network-fenced").is_file()
    for rule in ("iptables -A BOSS_SEALED -d 127.0.0.11/32 -j REJECT",
                 "iptables -A BOSS_SEALED -p tcp -m multiport --dports 8000,8090,8091 -j REJECT",
                 "iptables -A BOSS_SEALED -o lo -d 127.0.0.0/8 -j ACCEPT",
                 "iptables -A BOSS_SEALED -j REJECT",
                 "iptables -I OUTPUT 1 -m owner --uid-owner 61900-61999 -j BOSS_SEALED",
                 "iptables -A BOSS_SEALED -p tcp --dport 18100:18199 -j REJECT",
                 "ip6tables -A BOSS_SEALED -p tcp --dport 18100:18199 -j REJECT",
                 "ip6tables -A BOSS_SEALED -o lo -d ::1/128 -j ACCEPT"):
        assert rule in calls, rule
    # No compose network, no egress for the sealed chain.
    assert "BOSS_SEALED -d 172." not in calls


def test_a_failed_sealed_rule_leaves_no_sealed_marker(tmp_path):
    result, root, _ = run_entrypoint(tmp_path,
                                     ipv4_fail="-A BOSS_SEALED -o lo -d 127.0.0.0/8 -j ACCEPT")
    assert result.returncode == 0
    assert (root / ".network-fenced").is_file() and not (root / ".network-sealed").exists()
    assert "acceptance checks that run commands are refused" in result.stderr


def test_the_fenced_runner_never_runs_a_command_outside_the_sealed_uid(settings, monkeypatch):
    from boss_engine.services import owned_process
    from boss_engine.services.sandbox import SandboxError

    class Unfenced:
        sandbox = None
        _ended = False
        stdout = stderr = None

        def end(self, **kw):
            self._ended = True

    monkeypatch.setattr(owned_process, "popen", lambda *a, **k: Unfenced())
    runner = asn._FencedRunner(settings, "acceptance:gone")
    with pytest.raises(SandboxError, match="not run"):
        runner.run("make check", timeout=5)


ESCAPES = {
    "socket": ("python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), 5); "
               "print('ESCAPED')\""),
    "dns": "python3 -c \"import socket; socket.getaddrinfo('example.com', 443); print('ESCAPED')\"",
    "make_curl": "make net",
    "write_outside": "python3 -c \"open('{outside}/escaped', 'w').write('x'); print('ESCAPED')\"",
    "read_host": "python3 -c \"print('ESCAPED', open('{secret}').read())\"",
    "git_push": "make push",
    "link_port": ("python3 -c \"import socket; socket.create_connection(('127.0.0.1', {link}), 5); "
                  "print('ESCAPED')\""),
    "edit_tracked": "python3 -c \"open('Makefile', 'a').write('x'); print('ESCAPED')\"",
    "delete_tracked": "python3 -c \"import os; os.remove('app.py'); print('ESCAPED')\"",
}


@pytest.mark.skipif(not FENCED, reason="no sealed fence on this host: open_sandbox refuses "
                                       "(test_without_the_sealed_fence_...)")
@pytest.mark.parametrize("escape", sorted(ESCAPES))
def test_no_escape_from_the_sealed_sandbox(repo, settings, tmp_path, escape):
    (repo / "Makefile").write_text("net:\n\tcurl -sS -m 5 https://example.com\n"
                                   "push:\n\tgit push https://example.com/x.git HEAD\n")
    _git(repo, "commit", "-qam", "escape targets")
    import socket

    link = socket.socket()                     # BOSS's own listener on a link-gateway port
    link.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    link.bind(("127.0.0.1", 18150))
    link.listen(1)
    outside = tmp_path / "boss-owned"
    outside.mkdir(mode=0o700)
    secret = outside / ".env"
    secret.write_text("SECRET=1\n")
    secret.chmod(0o600)
    with asn.open_sandbox(settings, run_id=f"crun_esc_{escape}", source=repo) as sbx:
        cmd = sbx.exec(ESCAPES[escape].format(outside=outside, secret=secret, link=18150),
                       timeout=30)
        tracked = {n: (sbx.root / n).read_text() for n in ("Makefile", "app.py")}
    link.close()
    assert "x" not in tracked["Makefile"].splitlines()[-1:]
    said = f"{cmd.stdout}\n{cmd.stderr}"
    assert "ESCAPED" not in said
    assert cmd.refused or cmd.exit != 0
    # It really ran, in the clone, and was stopped by the fence — not a missing Makefile.
    assert not cmd.refused and "No rule to make target" not in said
    assert not (outside / "escaped").exists()


@pytest.mark.skipif(not FENCED, reason="no sealed fence on this host")
def test_the_sealed_session_has_its_own_temp_directory(repo, settings):
    with asn.open_sandbox(settings, run_id="crun_tmp", source=repo) as sbx:
        cmd = sbx.exec("python3 -c \"import tempfile; print(tempfile.gettempdir())\"")
    assert cmd.exit == 0 and cmd.stdout.strip().startswith(str(si.run_root()))


def test_the_clones_tracked_files_and_git_are_read_only_to_the_session(repo, tmp_path):
    clone = tmp_path / "c"
    asn._git(["clone", "--quiet", str(repo), str(clone)], tmp_path)
    (clone / "run.sh").write_text("#!/bin/sh\n")
    (clone / "run.sh").chmod(0o775)
    asn.protect_clone(clone)
    mode = lambda p: p.stat().st_mode & 0o7777  # noqa: E731
    assert mode(clone / "Makefile") == 0o644 and mode(clone / "run.sh") == 0o755
    assert mode(clone) == 0o1777                      # may add files, never replace ours
    assert mode(clone / ".git") == 0o755 and mode(clone / ".git" / "HEAD") == 0o644
    assert all(mode(Path(d)) == 0o755 for d, _, _ in os.walk(clone / ".git"))


# ── the record ──────────────────────────────────────────────────────────────────────────────

def test_every_command_is_recorded_with_exit_output_duration_and_tree_state(repo, settings,
                                                                           tmp_path):
    sbx = _sandbox(settings, repo, tmp_path)
    assert _git(sbx.root, "remote") == ""                     # nothing to push to
    ok = sbx.exec("make check")
    bad = sbx.exec("make fail")
    shell = sbx.exec("make check && make fail")
    push = sbx.exec("git push origin main")
    net = sbx.exec("python3 -c \"'https://example.com'\"")
    cat = sbx.exec("cat /etc/passwd")
    wrote = sbx.exec("python3 -c \"open('note.txt', 'w').write('x')\"")
    after = sbx.exec("make check")
    rec = sbx.record.to_list()
    sbx.close()
    assert [c["id"] for c in rec] == [f"E{i}" for i in range(1, 9)]
    assert ok.exit == 0 and "gateway-ok" in ok.stdout and ok.duration_ms >= 0
    assert ok.output_sha256 and ok.argv == ["make", "check"] and ok.tree_dirty is False
    assert bad.exit != 0 and "broken" in bad.stdout
    assert shell.refused and "shell" in shell.refused
    assert push.refused and "push" in push.refused
    assert net.refused and "example.com" in net.refused
    assert cat.refused and "allowlisted" in cat.refused
    # The tree AS EACH COMMAND FOUND IT: the write itself ran on a clean tree; the next did not.
    assert wrote.exit == 0 and wrote.tree_dirty is False and after.tree_dirty is True
    assert not sbx.scratch.exists()                          # nothing is kept


def test_output_is_trimmed_with_the_whole_outputs_digest(repo, settings, tmp_path):
    sbx = _sandbox(settings, repo, tmp_path)
    big = sbx.exec("python3 -c \"print('x' * 50000)\"")
    sbx.close()
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


def test_requests_reach_loopback_only_and_are_recorded_with_their_status(repo, settings, server,
                                                                        tmp_path):
    sbx = _sandbox(settings, repo, tmp_path)
    no = sbx.http("GET", f"{server}/payments")
    yes = sbx.http("GET", f"{server}/payments", headers={"Authorization": "Bearer good"})
    out = sbx.http("GET", "http://203.0.113.9/payments")
    sbx.close()
    assert no.status == 401 and "missing token" in no.stdout and no.exit == 0
    assert yes.status == 200 and no.tree_dirty is False
    assert out.refused and out.status is None
    target = _sandbox(settings, repo, tmp_path, egress=asn.LOOPBACK_TARGET,
                      target_hosts=("staging.example.invalid",))
    # A command naming another host never runs.
    assert target.refusal("python3 -c \"'https://other.example.invalid/x'\"")
    target.close()


def test_a_loopback_request_goes_through_the_sealed_runner_when_there_is_one(repo, settings,
                                                                            tmp_path):
    calls = []

    class Sealed(_TestRunner):
        def request(self, method, url, *, headers, body, timeout):
            calls.append((method, url))
            return 401, '{"error":"missing token"}', None

    sbx = _sandbox(settings, repo, tmp_path)
    sbx.runner = Sealed(settings, sbx.root)
    got = sbx.http("GET", "http://127.0.0.1:9/payments")
    sbx.close()
    assert calls == [("GET", "http://127.0.0.1:9/payments")] and got.status == 401


# ── the session: record, judge, replay ──────────────────────────────────────────────────────

JUDGE_MET = ('AC-1: MET — evidence: E1 (exit 0) "gateway-ok" and Makefile:2\n'
             "ACCEPTANCE: MET")


def _round_one(repo, settings, tmp_path, judge=JUDGE_MET):
    engine = Scripted(plan="AC-1 RUN make check\nRUN: make check", judge=judge)
    sbx = _sandbox(settings, repo, tmp_path)
    res = asn.run_session(sbx, items=ITEMS, ask=engine, title="Gateway")
    rec = av.judge(ITEMS, res.reply, {}, root=sbx.root, exec_log=res.record, replay=res.carried)
    probes = ap.from_record(rec, res.record, check_id="acc_round1")
    head = sbx.head_sha
    sbx.close()
    return engine, res, rec, probes, head


def test_round_one_runs_what_the_agent_planned_and_its_met_is_verified(repo, settings, tmp_path):
    engine, res, rec, probes, _ = _round_one(repo, settings, tmp_path)
    assert res.model_calls == 2 and len(engine.prompts) == 2
    assert "[E1] exit 0" in engine.prompts[1] and "gateway-ok" in engine.prompts[1]
    assert rec["verdict"] == "met" and rec["items"][0]["evidence_refs"][0]["verified"]
    assert probes == [{"item": "AC-1", "kind": "exec", "setup": [],
                       "action": {"cmd": "make check"},
                       "expect": {"exit": 0, "stdout_contains": "gateway-ok"},
                       "files": ["Makefile"],
                       "source": {"check_id": "acc_round1", "command_id": "E1"}}]


def test_a_met_on_a_command_alone_leaves_no_probe(repo, settings, tmp_path):
    """Nothing to watch, nothing to carry: a later check asks the agent again."""
    _, _, rec, probes, _ = _round_one(
        repo, settings, tmp_path,
        judge='AC-1: MET — evidence: E1 (exit 0) "gateway-ok"\nACCEPTANCE: MET')
    assert rec["verdict"] == "met" and probes == []


def test_a_recheck_replays_recorded_commands_first_with_no_model_call(repo, settings, tmp_path):
    _, _, _, probes, head = _round_one(repo, settings, tmp_path)
    sbx = _sandbox(settings, repo, tmp_path)
    res = asn.run_session(sbx, items=ITEMS, ask=_never, title="Gateway",
                          prior={"probes": probes, "head_sha": head})
    rec = av.judge(ITEMS, res.reply, {}, root=sbx.root, exec_log=res.record, replay=res.carried)
    commands = res.record.to_list()
    sbx.close()
    assert res.model_calls == 0 and set(res.carried) == {"AC-1"}
    assert commands[0]["replay"] is True and commands[0]["cmd"] == "make check"
    assert rec["verdict"] == "met" and rec["replayed"] == [1]


def test_an_unknown_diff_carries_nothing(repo, settings, tmp_path):
    _, _, _, probes, _ = _round_one(repo, settings, tmp_path)
    engine = Scripted(plan="RUN: make check", judge=JUDGE_MET)
    sbx = _sandbox(settings, repo, tmp_path)
    res = asn.run_session(sbx, items=ITEMS, ask=engine,
                          prior={"probes": probes, "head_sha": "0" * 40})   # not in the clone
    sbx.close()
    assert res.replayed["AC-1"]["result"] == "pass" and res.replayed["AC-1"]["changed"]
    assert res.carried == {} and res.model_calls == 2


def test_a_changed_file_or_a_failing_probe_sends_the_item_back_to_the_agent(repo, settings,
                                                                           tmp_path):
    _, _, _, probes, head = _round_one(repo, settings, tmp_path)
    (repo / "Makefile").write_text("check:\n\t@echo gateway-down\n")
    _git(repo, "commit", "-qam", "two")
    engine = Scripted(plan="RUN: make check",
                      judge='AC-1: MISSING — E2 (exit 0) "gateway-down"\nACCEPTANCE: NOT MET')
    sbx = _sandbox(settings, repo, tmp_path)
    res = asn.run_session(sbx, items=ITEMS, ask=engine, title="Gateway",
                          prior={"probes": probes, "head_sha": head})
    rec = av.judge(ITEMS, res.reply, {}, root=sbx.root, exec_log=res.record, replay=res.carried)
    sbx.close()
    assert res.replayed["AC-1"]["result"] == "fail" and res.replayed["AC-1"]["changed"]
    assert res.carried == {} and res.model_calls == 2
    assert rec["verdict"] == "not_met"
    assert av.diff({"items": [{"id": "AC-1", "status": "met"}]}, rec)["regressed"] == ["AC-1"]


def test_a_build_and_test_runs_own_output_never_spoils_its_evidence(repo, settings, tmp_path):
    """pytest's cache, a build directory the repo ignores: the command that made them, and
    every later one, is still evidence (review N2)."""
    (repo / ".gitignore").write_text("bin/\n")
    (repo / "Makefile").write_text("build:\n\tmkdir -p bin\n\tcp app.py bin/app\n"
                                   "check:\n\t@echo gateway-ok\n")
    (repo / "test_app.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "build + tests")
    sbx = _sandbox(settings, repo, tmp_path)
    tests = sbx.exec("python3 -m pytest -q test_app.py")
    build = sbx.exec("make build")
    check = sbx.exec("make check")
    again = sbx.exec("python3 -m pytest -q test_app.py")
    sbx.close()
    assert tests.exit == 0 and "1 passed" in tests.stdout
    assert [c.tree_dirty for c in (tests, build, check, again)] == [False] * 4
    items = [{"id": "AC-1", "n": 1, "source": "ticket", "text": "the tests pass"}]
    reply = 'AC-1: MET — E4 (exit 0) "1 passed" test_app.py:2\nACCEPTANCE: MET'
    rec = av.judge(items, reply, {}, root=sbx.root if sbx.root.exists() else tmp_path,
                   exec_log=sbx.record)
    assert rec["items"][0]["evidence_refs"][0]["verified"], rec["items"][0]


def test_evidence_after_the_session_changed_the_clone_is_not_accepted(repo, settings, tmp_path):
    """Write a file, then cite a command: the output may be the agent's own words. An item
    that can only be proven by editing is a person's (HUMAN), never a major unverified gap."""
    engine = Scripted(plan="RUN: python3 -c \"open('x.txt','w').write('1')\"\nRUN: make check",
                      judge='AC-1: MET — E2 (exit 0) "gateway-ok" Makefile:2\nACCEPTANCE: MET')
    sbx = _sandbox(settings, repo, tmp_path)
    res = asn.run_session(sbx, items=ITEMS, ask=engine)
    rec = av.judge(ITEMS, res.reply, {}, root=sbx.root, exec_log=res.record)
    sbx.close()
    item = rec["items"][0]
    assert item["status"] == "human" and "needs a person" in item["evidence"]
    assert rec["verdict"] == "not_met" and rec["unverified"] == []


def test_an_open_rows_verify_with_probe_that_passes_proves_it_fixed(repo, settings, tmp_path):
    rows = [{"number": 7, "title": "check target", "verify_with": [
        {"kind": "exec", "action": {"cmd": "make check"}, "files": ["Makefile"],
         "expect": {"exit": 0, "stdout_contains": "gateway-ok"}}]}]
    sbx = _sandbox(settings, repo, tmp_path)
    res = asn.run_session(sbx, items=[], ask=_never, open_rows=rows)
    sbx.close()
    assert res.finding_results == [{"finding": "F-7", "result": "pass", "command_ids": ["E1"]}]
    assert res.model_calls == 0


def test_the_model_is_never_asked_inside_an_open_transaction(repo, settings, tmp_path,
                                                            monkeypatch):
    from boss_engine.services.acceptance import contract

    monkeypatch.setattr(contract, "_transaction_open", lambda: True)
    engine = Scripted(plan="RUN: make check", judge="ACCEPTANCE: MET")
    sbx = _sandbox(settings, repo, tmp_path)
    monkeypatch.setenv(ac.STRICT_GUARD_ENV, "1")
    with pytest.raises(asn.ModelCallInTransaction):
        asn.run_session(sbx, items=ITEMS, ask=engine)
    monkeypatch.setenv(ac.STRICT_GUARD_ENV, "0")
    res = asn.run_session(sbx, items=ITEMS, ask=engine)
    sbx.close()
    assert engine.prompts == [] and res.stop and res.model_calls == 0


def test_planned_commands_read_run_and_http_lines_in_order():
    got = asn.planned_commands('PLAN...\nRUN: make build\nHTTP: GET http://127.0.0.1:8/x '
                               'headers {"Authorization": "Bearer t"}\n- RUN: `make test`\n')
    assert got == [{"kind": "exec", "cmd": "make build"},
                   {"kind": "http", "method": "GET", "url": "http://127.0.0.1:8/x",
                    "headers": {"Authorization": "Bearer t"}, "body": None},
                   {"kind": "exec", "cmd": "make test"}]


# ── Northgate v1: exec evidence on the real delivered tree ──────────────────────────────────

def test_northgate_run_staging_is_judged_on_what_the_command_really_printed(settings, tmp_path):
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
               'AC-1: MET — E1 (exit 0) "go build ./cmd/gateway"\nACCEPTANCE: NOT MET'))
    sbx = _sandbox(settings, bundle, tmp_path, branch=ng.expected["tree"]["branch"])
    res = asn.run_session(sbx, items=items, ask=engine, title="Northgate")
    rec = av.judge(items, res.reply, {}, root=sbx.root, exec_log=res.record)
    sbx.close()
    cmd = res.record.get("E1")
    assert cmd.exit == 0 and "docker compose" in cmd.stdout and cmd.tree_dirty is False
    by = {i["id"]: i for i in rec["items"]}
    assert by["AC-15"]["status"] == "missing"
    assert by["AC-1"]["status"] == "unjudged" and by["AC-1"]["unverified"]   # misquoted
    assert rec["verdict"] == "not_met"


# ── BOSSAPP-1316: a code-writing command taints what later commands read ────────────────────

@pytest.fixture()
def flag_repo(tmp_path: Path) -> Path:
    """check.py prints FEATURE ON when .env (gitignored) or ~/.flag says ON."""
    src = tmp_path / "flagrepo"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@example.invalid")
    _git(src, "config", "user.name", "t")
    (src / ".gitignore").write_text(".env\nnode_modules/\n")
    (src / "check.py").write_text(
        "import os, pathlib\n"
        "env = pathlib.Path('.env')\n"
        "home = pathlib.Path(os.environ['HOME']) / '.flag'\n"
        "on = any(p.exists() and p.read_text().strip() == 'ON' for p in (env, home))\n"
        "print('FEATURE ON' if on else 'FEATURE OFF')\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "one")
    return src


FLAG_ITEMS = [{"id": "AC-1", "n": 1, "source": "ticket", "text": "the feature is on"}]
FLAG_MET = 'AC-1: MET — E2 (exit 0) "FEATURE ON" check.py:5\nACCEPTANCE: MET'


@pytest.mark.parametrize("plant", [
    "python3 -c \"open('.env', 'w').write('ON')\"",                       # a gitignored file
    "python3 -c \"import os; os.makedirs('node_modules/x', exist_ok=True); "
    "open('node_modules/x/i.js', 'w').write('1'); open('.env', 'w').write('ON')\"",
])
def test_a_planted_ignored_file_never_makes_a_later_command_evidence(flag_repo, settings,
                                                                    tmp_path, plant):
    sbx = _sandbox(settings, flag_repo, tmp_path)
    planted = sbx.exec(plant)
    read = sbx.exec("python3 check.py")
    rec = av.judge(FLAG_ITEMS, FLAG_MET, {}, root=sbx.root, exec_log=sbx.record)
    sbx.close()
    assert planted.exit == 0 and "FEATURE ON" in read.stdout
    assert read.tree_dirty is False                      # the tracked tree IS clean
    assert read.tainted_by == "E1"
    assert rec["items"][0]["status"] == "unjudged" and "E1" in rec["items"][0]["evidence"]


def test_a_file_planted_in_the_sessions_home_taints_too(flag_repo, settings, tmp_path,
                                                       monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("ACC_TEST_HOME", str(home))
    sbx = _sandbox(settings, flag_repo, tmp_path, watch=(home,))
    sbx.exec(f"python3 -c \"open('{home}/.flag', 'w').write('ON')\"")
    read = sbx.exec("python3 check.py")
    rec = av.judge(FLAG_ITEMS, FLAG_MET, {}, root=sbx.root, exec_log=sbx.record)
    sbx.close()
    assert "FEATURE ON" in read.stdout and read.tainted_by == "E1"
    assert rec["items"][0]["status"] == "unjudged"


def test_a_code_command_that_writes_nothing_taints_nothing(flag_repo, settings, tmp_path):
    sbx = _sandbox(settings, flag_repo, tmp_path)
    sbx.exec("python3 -c \"import check\"")              # reads only (no bytecode written)
    read = sbx.exec("python3 check.py")
    rec = av.judge(FLAG_ITEMS, 'AC-1: MET — E2 (exit 0) "FEATURE OFF" check.py:5\n'
                               "ACCEPTANCE: MET", {}, root=sbx.root, exec_log=sbx.record)
    sbx.close()
    assert read.tainted_by is None and rec["items"][0]["status"] == "met"


# ── BOSSAPP-1318: loopback only to the session's own listeners ──────────────────────────────

def test_the_entrypoint_marks_each_sealed_uids_connections_and_rejects_foreign_replies(tmp_path):
    result, root, calls = run_entrypoint(tmp_path)
    assert result.returncode == 0 and (root / ".network-sealed").is_file(), result.stderr
    for rule in ("iptables -A BOSS_SEALED -o lo -j BOSS_SEALED_OWN",
                 "iptables -I OUTPUT 1 -o lo -j BOSS_SEALED_REPLY",
                 "ip6tables -A BOSS_SEALED -o lo -j BOSS_SEALED_OWN",
                 "ip6tables -I OUTPUT 1 -o lo -j BOSS_SEALED_REPLY"):
        assert rule in calls, rule
    rules = subprocess.run(["/bin/sh", "-c", _sealed_rules_script()], capture_output=True,
                           text=True, check=True).stdout
    assert ("-A BOSS_SEALED_OWN -m owner --uid-owner 61900 -m conntrack --ctstate NEW "
            f"-j CONNMARK --set-xmark {0x5EA10000}/0xffffffff") in rules
    assert (f"-A BOSS_SEALED_REPLY -m connmark --mark {0x5EA10000 + 99}/0xffffffff "
            "-m owner ! --uid-owner 61999 -j REJECT") in rules
    assert rules.count("-A BOSS_SEALED_REPLY") == 100


def _sealed_rules_script() -> str:
    src = (Path(__file__).resolve().parents[2] / "scripts/sandbox/entrypoint.sh").read_text()
    body = src[src.index("sealed_own_rules() {"):src.index("seal_network() {")]
    return 'SEALED_RANGE=61900-61999\n' + body + "sealed_own_rules\n"


@pytest.mark.skipif(not FENCED, reason="no sealed fence on this host")
def test_another_uids_listener_is_unreachable_and_the_sessions_own_server_answers(
        repo, settings, tmp_path):
    import socket

    (repo / "own.py").write_text(
        "import socket, threading\n"
        "srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(1)\n"
        "def serve():\n"
        "    c, _ = srv.accept(); c.sendall(b'OWN-OK'); c.close()\n"
        "threading.Thread(target=serve, daemon=True).start()\n"
        "cli = socket.create_connection(srv.getsockname(), 5)\n"
        "print(cli.recv(16).decode())\n")
    (repo / "probe.py").write_text(
        "import socket, sys\n"
        "try:\n"
        "    c = socket.create_connection(('127.0.0.1', int(sys.argv[1])), 5)\n"
        "    print('REACHED', c.recv(16))\n"
        "except OSError as e:\n"
        "    print('BLOCKED', type(e).__name__)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "net probes")
    other = socket.socket()                     # BOSS's uid: a listener on a random port
    other.bind(("127.0.0.1", 0))
    other.listen(1)
    port = other.getsockname()[1]

    def answer():
        try:
            c, _ = other.accept()
            c.sendall(b"HELLO-OTHER")
            c.close()
        except OSError:
            pass
    threading.Thread(target=answer, daemon=True).start()
    with asn.open_sandbox(settings, run_id="crun_lo", source=repo) as sbx:
        own = sbx.exec("python3 own.py", timeout=30)
        foreign = sbx.exec(f"python3 probe.py {port}", timeout=30)
    other.close()
    assert own.exit == 0 and "OWN-OK" in own.stdout, own
    assert "BLOCKED" in foreign.stdout and "HELLO-OTHER" not in foreign.stdout, foreign
