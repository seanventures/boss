"""Overrides of validation findings and the automatic re-run (SPEC docs/design/validation-review
§3, §7 PR 3 (a)–(e); §4 rulesets, §7 PR 4 (a)).

The change is Northgate's change #9 with Lumen Pay's real partner file and gwlint's real outputs
(engine/tests/fixtures/validation). The repository host is an in-memory branch (every write is a
new head) so the tests need no git or Go; everything else — the tables, the decision, the job,
the packet — is the real path.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from apps.api.tests import command_center_view_seed as seed
from boss_engine.services import finding_overrides as fo
from boss_engine.services import input_fidelity as f
from boss_engine.services import review_packet as rp

PID = seed.PROJECT
FIX = Path(__file__).resolve().parents[3] / "engine" / "tests" / "fixtures" / "validation"
INPUT, TARGET = "examples/lumen-pay.bad.yaml", "config/tenants/lumen-pay.yaml"
RULESET = """\
ruleset: northgate
version: 3
validator: gwlint
rules:
  no-inlined-secrets: {severity: P0, autofix: never, guide: "Delete the key if the route already authenticates another way."}
  cors-no-wildcard-with-credentials: {severity: P0, autofix: agent, guide: "Default to allow_credentials: false."}
  no-sensitive-paths-exposed: {severity: P0, autofix: human}
  upstream-https-required: {severity: P0, autofix: mechanical}
  tenant-rate-limit-required: {severity: P1, autofix: agent, guide: "rps 50, burst 50 unless the brief names a quota."}
  rate-limit-required: {severity: P1, autofix: mechanical}
  request-size-capped: {severity: P1, autofix: mechanical}
  pii-fields-declared: {severity: P2, autofix: agent}
"""


class Branch:
    """A change branch: files by path; every commit is a new head."""

    def __init__(self, files):
        self.files, self.commits = dict(files), []

    @property
    def sha(self) -> str:
        return hashlib.sha1(json.dumps(self.files, sort_keys=True).encode()).hexdigest()

    def read_text_file(self, branch, path):
        return self.files.get(path)

    def upsert_file(self, branch, path, content, message):
        self.files[path] = content
        self.commits.append(message)
        return self.sha


def _tree(root: Path, files) -> Path:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    return root


def _gwlint(root: Path):
    """gwlint replayed from its real outputs, by the file's content."""
    bad, fixed = (FIX / "lumen-pay.bad.yaml").read_text(), (FIX / "lumen-pay.fixed.yaml").read_text()

    def run(cmd):
        if "--fix" in cmd:
            (root / cmd.split()[-1]).write_text(fixed)
            return SimpleNamespace(stdout="", exit_code=1)
        body = (root / cmd.split()[-2]).read_text()
        if body == bad:
            return SimpleNamespace(stdout=(FIX / "gwlint.before.json").read_text(), exit_code=1)
        if body == fixed:
            return SimpleNamespace(stdout=(FIX / "gwlint.after.json").read_text(), exit_code=1)
        return SimpleNamespace(stdout='{"path": "x", "violations": []}', exit_code=0)
    return run


def _files() -> dict:
    return {INPUT: (FIX / "lumen-pay.bad.yaml").read_text(),
            TARGET: (FIX / "lumen-pay.fixed.yaml").read_text(),
            ".boss/validators.yaml": (FIX / "validators.yaml").read_text(),
            ".boss/rulesets/northgate.yaml": RULESET}


def _validation(tmp_path: Path, files) -> dict:
    root = _tree(tmp_path / f"v{uuid.uuid4().hex[:6]}", {k: v for k, v in files.items() if k != TARGET})
    c = f.contract({"remediate": {"input": INPUT, "output": TARGET}}, "", root)
    (root / TARGET).parent.mkdir(parents=True, exist_ok=True)
    (root / TARGET).write_text(files[TARGET])
    return f.public_validation(f.run_validation(root, c, _gwlint(root)))


@pytest.fixture
def change(engine, monkeypatch, tmp_path):
    files = _files()
    branch = Branch(files)
    rec = _validation(tmp_path, files)
    number = 9000 + int(uuid.uuid4().hex[:3], 16) % 999
    rid = f"crun_{uuid.uuid4().hex[:16]}"
    url = f"http://boss-local/acme/northgate/pull/{number}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, finalize_ctx, "
            "state, pull_request_url) VALUES (:i, :p, 'completed', 'done', "
            "'Validate tenant config with gwlint', '{}'::jsonb, '{}'::jsonb, CAST(:s AS JSONB), :u)"),
            {"i": rid, "p": PID, "s": json.dumps({"validation": rec}), "u": url})
        did = f"dec_{uuid.uuid4().hex[:12]}"
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status) "
            "VALUES (:i, :p, 'Approve code change', CAST(:r AS JSONB), 'pending')"),
            {"i": did, "p": PID, "r": json.dumps({"action": "review_pull_request", "run_id": rid,
                                                   "pull_request_url": url})})
    ch = fo.Change(PID, number, rid, "boss/code/x", url, branch, lambda: branch.sha)
    monkeypatch.setattr(fo, "change", lambda c, s, p, n: ch)
    return SimpleNamespace(branch=branch, number=number, run_id=rid, decision=did, ch=ch,
                           tmp=tmp_path, url=url)


def _id(state: dict, rule: str) -> str:
    return next(x["id"] for x in state["validation"]["findings"]
                if x["source"]["rule_id"] == rule)


def _get(client, ch) -> dict:
    got = client.get(f"/v1/changes/{PID}/{ch.number}/validation")
    assert got.status_code == 200, got.text
    return got.json()


def _override(client, ch, fid, **body):
    return client.post(f"/v1/changes/{PID}/{ch.number}/findings/{fid}/override",
                       json={"head_sha": ch.branch.sha, **body})


def _finish_rerun(engine, ch, rerun_id: str) -> dict:
    """The worker's re-run, with the checkout and gwlint seams."""
    return fo.execute_rerun(
        engine, None, rerun_id,
        do_checkout=lambda change, tree: _tree(tree, ch.branch.files),
        make_run=lambda tree: _gwlint(tree))


# ── the read ───────────────────────────────────────────────────────────────────────────────────

def test_the_validation_read_has_the_record_the_ruleset_p_levels_and_packet_v1(client, change):
    state = _get(client, change)
    assert state["validation"]["schema"] == "boss.validation/v1"
    assert state["validation"]["ruleset"]["id"] == "northgate"
    p = state["packet"]
    assert p["packet_version"] == 1 and p["head_sha"] == change.branch.sha
    assert p["counts"]["P0"] == {"found": 4, "open": 3, "waived": 0}
    assert p["validators"][0]["ruleset"] == "northgate v3"
    admin = next(x for x in p["findings"] if x["source"]["rule_id"] == "no-sensitive-paths-exposed")
    assert admin["fix_kind"] == "human" and admin["basis"] == "ruleset northgate v3"
    assert state["overrides"] == [] and state["rerun"] is None


# ── (e) a stale head ───────────────────────────────────────────────────────────────────────────

def test_e_a_stale_head_sha_is_409_with_the_newer_packet(client, change):
    fid = _id(_get(client, change), "pii-fields-declared")
    got = client.post(f"/v1/changes/{PID}/{change.number}/findings/{fid}/override",
                      json={"action": "waive", "reason": "no PII", "head_sha": "0000000"})
    assert got.status_code == 409, got.text
    body = got.json()
    assert body["error"] == "stale_head" and body["head_sha"] == change.branch.sha
    assert body["packet"]["schema"] == "boss.review_packet/v2"


# ── (a) reject restores the partner's exact bytes ──────────────────────────────────────────────

def test_a_reject_restores_the_partners_exact_bytes_for_that_hunk(client, change):
    fid = _id(_get(client, change), "upstream-https-required")
    got = _override(client, change, fid, action="reject", reason="partner's upstream is internal")
    assert got.status_code == 200, got.text
    target = change.branch.files[TARGET]
    want = (FIX / "lumen-pay.fixed.yaml").read_text().replace(
        'url: "https://lumen-pay-core', 'url: "http://lumen-pay-core')
    assert target.encode() == want.encode()
    msg = change.branch.commits[-1]
    assert f"BOSS-Override: O-1 {fid} reject" in msg
    assert "On-behalf-of: " in msg and "Reason: partner's upstream is internal" in msg
    assert got.json()["override"]["commit_sha"] == change.branch.sha


def test_edit_writes_the_persons_line_and_accept_writes_no_commit(engine, client, change):
    fid = _id(_get(client, change), "upstream-https-required")
    got = _override(client, change, fid, action="edit",
                    text='    url: "https://core.lumenpay.example"')
    assert got.status_code == 200, got.text
    assert '    url: "https://core.lumenpay.example"' in change.branch.files[TARGET].splitlines()
    assert "BOSS-Override: O-1" in change.branch.commits[-1]
    # While the re-run runs, nothing else is accepted against the moving packet.
    assert _override(client, change, fid, action="accept").json()["error"] == "rerun_pending"
    assert _finish_rerun(engine, change, got.json()["rerun"]["id"])["state"] == "done"
    commits, head = len(change.branch.commits), change.branch.sha
    assert _override(client, change, fid, action="accept").status_code == 200
    assert len(change.branch.commits) == commits and change.branch.sha == head


# ── (b) revert-all ─────────────────────────────────────────────────────────────────────────────

def test_b_revert_all_makes_the_target_the_partners_bytes(client, change):
    got = client.post(f"/v1/changes/{PID}/{change.number}/validation/revert-all",
                      json={"head_sha": change.branch.sha, "reason": "start over from the partner"})
    assert got.status_code == 200, got.text
    sha = lambda t: hashlib.sha256(t.encode()).hexdigest()  # noqa: E731
    assert sha(change.branch.files[TARGET]) == sha(change.branch.files[INPUT])
    assert "BOSS-Override: O-1 * revert_all" in change.branch.commits[-1]


# ── (c) a P0 waive needs a second person ───────────────────────────────────────────────────────

def test_c_a_p0_waive_without_an_approver_is_403(client, change):
    fid = _id(_get(client, change), "cors-no-wildcard-with-credentials")
    got = _override(client, change, fid, action="waive", reason="dashboard needs cookies")
    assert got.status_code == 403 and got.json()["error"] == "approver_required"


def test_c_with_an_approver_it_creates_a_decision_and_stays_open_until_approved(
        engine, client, change):
    fid = _id(_get(client, change), "cors-no-wildcard-with-credentials")
    got = _override(client, change, fid, action="waive", reason="dashboard needs cookies",
                    approver_id="usr_security_lead")
    assert got.status_code == 200, got.text
    did = got.json()["override"]["approval_decision"]
    assert did
    with engine.begin() as c:
        rec = c.execute(text("SELECT recommendation FROM decisions WHERE id = :i"),
                        {"i": did}).scalar()
    assert rec["action"] == "waive_finding" and rec["approver"] == "usr_security_lead"
    p = got.json()["packet"]
    assert next(x for x in p["findings"] if x["id"] == fid)["status"] == "open"
    with engine.begin() as c, pytest.raises(fo.OverrideError):
        fo.on_waive_approved(c, None, rec, "usr_someone_else")
    with engine.begin() as c:
        fo.on_waive_approved(c, None, rec, "usr_security_lead")
        p = fo.packet(c, change.ch)
    f2 = next(x for x in p["findings"] if x["id"] == fid)
    assert f2["status"] == "waived" and f2["override"]["id"] == "O-1"
    assert f2["override"]["approver"] == "usr_security_lead"


# ── (d) the re-run: queued at once, merge refused until it is done, packet v(n+1) ──────────────

def test_d_an_override_queues_the_rerun_and_the_merge_waits_for_it(engine, client, change):
    fid = _id(_get(client, change), "pii-fields-declared")
    got = _override(client, change, fid, action="waive",
                    reason="no PII in query params, partner confirmed, KAN-12")
    assert got.status_code == 200, got.text
    rerun = got.json()["rerun"]["id"]
    with engine.begin() as c:
        job = c.execute(text("SELECT kind, payload FROM jobs WHERE kind = 'validation.rerun' "
                             "AND payload->>'rerun_id' = :r"), {"r": rerun}).mappings().first()
        card = c.execute(text("SELECT recommendation FROM decisions WHERE id = :i"),
                         {"i": change.decision}).scalar()
    assert job is not None
    assert card["packet"]["verdict"]["reasons"][-1] == "rerun_pending"
    assert rp.blocks_merge(card, force=True)[0] == "rerun_pending"
    assert _override(client, change, fid, action="reopen").json()["error"] == "rerun_pending"

    out = _finish_rerun(engine, change, rerun)
    assert out["state"] == "done", out
    with engine.begin() as c:
        card = c.execute(text("SELECT recommendation FROM decisions WHERE id = :i"),
                         {"i": change.decision}).scalar()
    p = card["packet"]
    assert p["packet_version"] == 2 and "rerun_pending" not in p["verdict"]["reasons"]
    pii = next(x for x in p["findings"] if x["id"] == fid)
    assert pii["status"] == "waived" and pii["override"]["reason"].endswith("KAN-12")
    assert rp.validate_packet(p) == []
    assert any(o["id"] == "O-1" for o in p["overrides"])


# ── §4 (PR 4 a): a ruleset P-level change moves the packet ─────────────────────────────────────

def test_a_ruleset_raising_pii_to_p1_makes_the_packet_block_on_it(tmp_path):
    files = _files()
    files[".boss/rulesets/northgate.yaml"] = RULESET.replace(
        "pii-fields-declared: {severity: P2", "pii-fields-declared: {severity: P1")
    rec = _validation(tmp_path, files)
    p = rp.build_packet(work={"title": "t"}, head_sha="x", validations=[rec])
    pii = next(x for x in p["findings"] if x["source"]["rule_id"] == "pii-fields-declared")
    assert pii["severity"] == "P1" and f"open_p1:{pii['id']}" in p["verdict"]["reasons"]


# ── §4 (PR 4 b): the guide reaches the fixing agent verbatim ───────────────────────────────────

def test_a_guide_edit_appears_verbatim_in_the_fixing_agents_brief(tmp_path):
    guide = "Replace it with api_key_secret_ref: vault://lumen-pay/payments. Never hash it."
    files = _files()
    files[".boss/rulesets/northgate.yaml"] = RULESET.replace(
        "Delete the key if the route already authenticates another way.", guide)
    root = _tree(tmp_path / "g", files)
    brief = f.brief(f.contract({"remediate": {"input": INPUT, "output": TARGET}}, "", root))
    assert f"- no-inlined-secrets [P0, autofix never]: {guide}" in brief


def test_a_waived_override_never_edits_the_validators_output(engine, client, change):
    before = json.dumps(_get(client, change)["validation"], sort_keys=True)
    fid = _id(_get(client, change), "pii-fields-declared")
    assert _override(client, change, fid, action="waive", reason="partner confirmed").status_code == 200
    assert json.dumps(_get(client, change)["validation"], sort_keys=True) == before


def test_the_schema_declares_both_tables():
    schema = (Path(__file__).resolve().parents[3] / "db" / "schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS finding_overrides" in schema
    assert "CREATE TABLE IF NOT EXISTS validation_reruns" in schema
