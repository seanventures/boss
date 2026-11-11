"""UNKNOWN_OUTCOME is a first-class state; an irreversible op never blind-retries (BOSSAPP-986).

The cousins on record: BOSSAPP-975 (parked-run authority) and 982 (approve-then-silent-revert).
The shape is always the same — a worker dies, or the call times out, BETWEEN asking the
provider for an irreversible effect (a merge, a delete, a delivered message) and recording the
answer. Retrying blind can double-apply; declaring failure can lie. So:

  * every externally effectful job kind declares its side-effect class
    (durable_operations.JOB_KIND_CLASSES), and its effect dedupes on a stable operation id
    (the receipt dedupe_key);
  * an irreversible kind with an ambiguous failure parks in unknown_outcome — at fail() when
    the worker reports, at claim when the worker died silently, at reap when nobody claims;
  * only the reconciler, having ASKED the provider what happened, releases it — to a retry
    the effect seam recognises as settled (no second merge), or to an honest first try.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import durable_operations as ops
from boss_engine.services.core import now_utc
from boss_engine.services.jobs import JobLeaseService, enqueue_job
from boss_engine.services.receipts import ReceiptService

from ._ambient_support import eng, make_project, project, purge  # noqa: F401

MERGE = "github.pull_request.merge"


def _merge_payload(decision="dec_1") -> dict:
    return {"decision_id": decision, "dedupe_key": f"approved-pr-merge-{decision}",
            "pull_request_number": 7,
            "pull_request_url": "https://github.com/acme/app/pull/7", "title": "Ship it"}


def _lease(eng, pid, jid, worker="worker-a", ttl=60):
    with eng.begin() as c:
        return JobLeaseService(get_settings()).lease_owned_job(
            c, project_id=pid, job_id=jid, worker_id=worker, lease_ttl_seconds=ttl)


def _receipt(eng, pid, jid, dedupe_key):
    with eng.begin() as c:
        org, ws = c.execute(text(
            "SELECT w.org_id, w.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
            "WHERE p.id = :p"), {"p": pid}).one()
        receipt, _created = ReceiptService().create_or_reuse(
            c, org, ws, pid, dedupe_key, MERGE, {"pull_request_number": 7},
            {"title": "Ship it"}, created_by_job_id=jid)
        return receipt["id"]


def _job_state(eng, jid):
    with eng.begin() as c:
        return dict(c.execute(text(
            "SELECT status, run_after, last_error, attempt FROM jobs WHERE id = :j"),
            {"j": jid}).mappings().one())


def _cleanup(eng, pid):
    with eng.begin() as c:
        c.execute(text("DELETE FROM deadletters WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM receipts WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": pid})


# --------------------------------------------------------------------------- #
# The declarations themselves
# --------------------------------------------------------------------------- #

def test_every_declared_class_is_one_of_the_four():
    for kind, klass in ops.JOB_KIND_CLASSES.items():
        assert klass in ops.SIDE_EFFECT_CLASSES, f"{kind} declares unknown class {klass}"
    assert ops.side_effect_class(MERGE) == ops.IRREVERSIBLE
    assert ops.side_effect_class("infra.droplet.delete") == ops.IRREVERSIBLE
    assert ops.side_effect_class("comms.outbox.deliver") == ops.IRREVERSIBLE
    assert ops.side_effect_class("not.a.kind") is None


def test_ambiguity_is_classified_conservatively():
    assert ops.ambiguous_outcome({"type": "ReadTimeout", "message": "read timed out"})
    assert ops.ambiguous_outcome({"message": "502 Bad Gateway from api.github.com"})
    assert ops.ambiguous_outcome({"message": "Connection reset by peer"})
    # Knowable "did not happen": the request never got through, or was refused outright.
    assert not ops.ambiguous_outcome({"message": "Connection refused"})
    assert not ops.ambiguous_outcome({"message": "422 Unprocessable Entity: base moved"})
    assert not ops.ambiguous_outcome({"message": "403 Forbidden: resource not accessible"})
    assert not ops.ambiguous_outcome({"message": ""})


def test_the_effect_dedupes_on_its_stable_operation_id(eng, project):  # noqa: F811
    """Dispatch dedupe: the same operation id never runs the effect twice, and the receipt
    carries the declared class."""
    key = f"approved-pr-merge-{uuid4().hex[:8]}"
    try:
        with eng.begin() as c:
            org, ws = c.execute(text(
                "SELECT w.org_id, w.id FROM projects p JOIN workspaces w "
                "ON w.id = p.workspace_id WHERE p.id = :p"), {"p": project}).one()
            first, created_first = ReceiptService().create_or_reuse(
                c, org, ws, project, key, MERGE, {}, {"title": "x"})
            again, created_again = ReceiptService().create_or_reuse(
                c, org, ws, project, key, MERGE, {}, {"title": "x"})
        assert created_first is True and created_again is False
        assert again["id"] == first["id"]
        with eng.begin() as c:
            klass = c.execute(text("SELECT side_effect_class FROM receipts WHERE id = :r"),
                              {"r": first["id"]}).scalar()
        assert klass == ops.IRREVERSIBLE
    finally:
        _cleanup(eng, project)


# --------------------------------------------------------------------------- #
# Kill the worker at each stage
# --------------------------------------------------------------------------- #

def test_an_ambiguous_merge_failure_parks_instead_of_retrying(eng, project):  # noqa: F811
    """Stage 1: the worker survives but its call's outcome is unknown. fail() must park."""
    with eng.begin() as c:
        jid = enqueue_job(c, project, MERGE, _merge_payload(), max_attempts=3)["id"]
    try:
        lease = _lease(eng, project, jid)
        rcpt = _receipt(eng, project, jid, _merge_payload()["dedupe_key"])
        with eng.begin() as c:
            out = JobLeaseService(get_settings()).fail(
                c, "worker-a", jid, lease["lease_token"],
                {"type": "ReadTimeout", "message": "HTTPSConnectionPool: read timed out"},
                True, 5)
        assert out["held"] == "unknown_outcome"
        state = _job_state(eng, jid)
        assert state["status"] == "failed_retryable"
        assert state["last_error"]["code"] == "unknown_outcome"
        # Out of every claim query until the reconciler answers: days away, not seconds.
        assert state["run_after"] > now_utc() + timedelta(days=1)
        with eng.begin() as c:
            assert c.execute(text("SELECT status FROM receipts WHERE id = :r"),
                             {"r": rcpt}).scalar() == "unknown_outcome"
        # And no worker can claim it now.
        with eng.begin() as c:
            claimed = JobLeaseService(get_settings()).claim_job(c, "worker-b", [MERGE], 60)
        assert not (claimed and claimed["id"] == jid)
    finally:
        _cleanup(eng, project)


def test_a_knowable_failure_still_retries_normally(eng, project):  # noqa: F811
    with eng.begin() as c:
        jid = enqueue_job(c, project, MERGE, _merge_payload("dec_2"), max_attempts=3)["id"]
    try:
        lease = _lease(eng, project, jid)
        with eng.begin() as c:
            out = JobLeaseService(get_settings()).fail(
                c, "worker-a", jid, lease["lease_token"],
                {"type": "RuntimeError", "message": "409: base branch was modified"}, True, 5)
        assert out["status"] == "failed_retryable"
        state = _job_state(eng, jid)
        assert state["last_error"].get("code") != "unknown_outcome"
        assert state["run_after"] < now_utc() + timedelta(minutes=5)
    finally:
        _cleanup(eng, project)


def test_a_dead_workers_merge_is_parked_at_reclaim_not_rerun(eng, project):  # noqa: F811
    """Stage 2: the worker dies silently mid-merge. The successor must NOT double-apply."""
    with eng.begin() as c:
        jid = enqueue_job(c, project, MERGE, _merge_payload("dec_3"), max_attempts=3)["id"]
    try:
        _lease(eng, project, jid, worker="worker-dead")
        rcpt = _receipt(eng, project, jid, _merge_payload("dec_3")["dedupe_key"])
        with eng.begin() as c:
            c.execute(text("UPDATE jobs SET lease_expires_at = :exp WHERE id = :j"),
                      {"j": jid, "exp": now_utc() - timedelta(minutes=6)})
        # Worker B polls. It must not receive this job; the job parks instead.
        for _ in range(10):
            with eng.begin() as c:
                claimed = JobLeaseService(get_settings()).claim_job(c, "worker-b", [MERGE], 60)
            if claimed is None:
                break
            assert claimed["id"] != jid, "a dead worker's merge must never be re-leased blind"
        state = _job_state(eng, jid)
        assert state["status"] == "failed_retryable"
        assert state["last_error"]["code"] == "unknown_outcome"
        with eng.begin() as c:
            assert c.execute(text("SELECT status FROM receipts WHERE id = :r"),
                             {"r": rcpt}).scalar() == "unknown_outcome"
    finally:
        _cleanup(eng, project)


def test_the_reaper_parks_an_abandoned_irreversible_lease(eng, project):  # noqa: F811
    """Stage 3: nobody even polls for the kind. The reaper parks it, never requeues blind —
    and never deadletters it as 'failed', which could be a lie."""
    with eng.begin() as c:
        jid = enqueue_job(c, project, MERGE, _merge_payload("dec_4"), max_attempts=3)["id"]
    try:
        _lease(eng, project, jid, worker="worker-dead")
        with eng.begin() as c:
            c.execute(text("UPDATE jobs SET lease_expires_at = :exp, attempt = 3 "
                           "WHERE id = :j"),
                      {"j": jid, "exp": now_utc() - timedelta(minutes=120)})
        with eng.begin() as c:
            out = JobLeaseService(get_settings()).reap_lost_leases(c, actor="reaper")
        assert out["held_unknown"] >= 1
        state = _job_state(eng, jid)
        assert state["status"] == "failed_retryable"
        assert state["last_error"]["code"] == "unknown_outcome"
        with eng.begin() as c:
            dlq = c.execute(text("SELECT count(*) FROM deadletters WHERE project_id = :p "
                                 "AND source_id = :j"), {"p": project, "j": jid}).scalar()
        assert int(dlq) == 0, "an unknown outcome is not a known failure"
    finally:
        _cleanup(eng, project)


def test_an_idempotent_kind_still_reclaims_normally(eng, project):  # noqa: F811
    """The park rule is the exception, not the plane: a safe kind keeps today's behavior."""
    with eng.begin() as c:
        jid = enqueue_job(c, project, "repo.push_pending", {"branch": "feat/x"},
                          max_attempts=3)["id"]
    try:
        _lease(eng, project, jid, worker="worker-dead")
        with eng.begin() as c:
            c.execute(text("UPDATE jobs SET lease_expires_at = :exp WHERE id = :j"),
                      {"j": jid, "exp": now_utc() - timedelta(minutes=6)})
        reclaimed = None
        for _ in range(10):
            with eng.begin() as c:
                reclaimed = JobLeaseService(get_settings()).claim_job(
                    c, "worker-b", ["repo.push_pending"], 60)
            if reclaimed and reclaimed["id"] == jid:
                break
        assert reclaimed and reclaimed["id"] == jid
    finally:
        _cleanup(eng, project)


# --------------------------------------------------------------------------- #
# The reconciler: the provider answers, then — and only then — the job moves
# --------------------------------------------------------------------------- #

def _park(eng, pid, decision="dec_r") -> tuple[str, str]:
    with eng.begin() as c:
        jid = enqueue_job(c, pid, MERGE, _merge_payload(decision), max_attempts=3)["id"]
    lease = _lease(eng, pid, jid)
    rcpt = _receipt(eng, pid, jid, _merge_payload(decision)["dedupe_key"])
    with eng.begin() as c:
        JobLeaseService(get_settings()).fail(
            c, "worker-a", jid, lease["lease_token"],
            {"type": "ReadTimeout", "message": "read timed out"}, True, 5)
    # Make the first ask due now.
    with eng.begin() as c:
        c.execute(text(
            "UPDATE jobs SET last_error = last_error || jsonb_build_object("
            "'reconcile_after', CAST(:t AS TEXT)) WHERE id = :j"),
            {"j": jid, "t": (now_utc() - timedelta(seconds=1)).isoformat()})
    return jid, rcpt


def test_the_reconciler_settles_a_merge_that_landed(eng, project):  # noqa: F811
    jid, rcpt = _park(eng, project, "dec_r1")
    try:
        out = ops.reconcile_unknown_outcomes(
            eng, get_settings(),
            reconcilers={MERGE: lambda c, s, job, payload: ("happened",
                                                            {"merged_sha": "abc123"})})
        assert out["resolved_happened"] == 1
        state = _job_state(eng, jid)
        assert state["last_error"]["code"] == "unknown_outcome_resolved"
        assert state["run_after"] <= now_utc()
        with eng.begin() as c:
            status, result = c.execute(text(
                "SELECT status, result FROM receipts WHERE id = :r"), {"r": rcpt}).one()
        assert status == "succeeded"
        assert (result or {}).get("merged_sha") == "abc123"
        # Released: a worker may claim it again, and the effect seam's settled check makes
        # that retry bookkeeping, not a second merge.
        claimed = None
        for _ in range(10):
            with eng.begin() as c:
                claimed = JobLeaseService(get_settings()).claim_job(c, "worker-b", [MERGE], 60)
            if claimed and claimed["id"] == jid:
                break
        assert claimed and claimed["id"] == jid
    finally:
        _cleanup(eng, project)


def test_the_reconciler_releases_a_merge_that_never_happened(eng, project):  # noqa: F811
    jid, rcpt = _park(eng, project, "dec_r2")
    try:
        out = ops.reconcile_unknown_outcomes(
            eng, get_settings(),
            reconcilers={MERGE: lambda c, s, job, payload: ("did_not_happen",
                                                            {"pull_request_state": "open"})})
        assert out["resolved_did_not_happen"] == 1
        state = _job_state(eng, jid)
        assert state["run_after"] <= now_utc()
        with eng.begin() as c:
            assert c.execute(text("SELECT status FROM receipts WHERE id = :r"),
                             {"r": rcpt}).scalar() == "failed"
    finally:
        _cleanup(eng, project)


def test_an_unanswered_ask_backs_off_and_finally_goes_to_a_person(eng, project):  # noqa: F811
    jid, _rcpt = _park(eng, project, "dec_r3")
    try:
        out = ops.reconcile_unknown_outcomes(
            eng, get_settings(),
            reconcilers={MERGE: lambda c, s, job, payload: ("unknown",
                                                            {"note": "host unreachable"})})
        assert out["still_unknown"] == 1
        state = _job_state(eng, jid)
        assert state["last_error"]["reconcile_attempts"] == 1
        assert state["last_error"]["code"] == "unknown_outcome"

        # Every ask went unanswered: the job ends with a person asked, in words.
        with eng.begin() as c:
            c.execute(text(
                "UPDATE jobs SET last_error = last_error || jsonb_build_object("
                "'reconcile_attempts', :n, 'reconcile_after', CAST(:t AS TEXT)) "
                "WHERE id = :j"),
                {"j": jid, "n": ops.RECONCILE_MAX_ATTEMPTS - 1,
                 "t": (now_utc() - timedelta(seconds=1)).isoformat()})
        out = ops.reconcile_unknown_outcomes(
            eng, get_settings(),
            reconcilers={MERGE: lambda c, s, job, payload: ("unknown",
                                                            {"note": "host unreachable"})})
        assert out["retired"] == 1
        state = _job_state(eng, jid)
        assert state["status"] == "deadlettered"
        with eng.begin() as c:
            reason = c.execute(text("SELECT reason FROM deadletters WHERE source_id = :j"),
                               {"j": jid}).scalar()
        assert "outcome is unknown" in str(reason)
        assert "person" in str(reason)
    finally:
        _cleanup(eng, project)


def test_a_job_with_no_reconciler_waits_rather_than_retries(eng, project):  # noqa: F811
    """An irreversible kind nobody can reconcile yet stays parked — never released blind."""
    with eng.begin() as c:
        jid = enqueue_job(c, project, "infra.droplet.delete", {"resource_id": "res_1"},
                          max_attempts=3)["id"]
    try:
        lease = _lease(eng, project, jid)
        with eng.begin() as c:
            JobLeaseService(get_settings()).fail(
                c, "worker-a", jid, lease["lease_token"],
                {"type": "ReadTimeout", "message": "read timed out"}, True, 5)
        with eng.begin() as c:
            c.execute(text(
                "UPDATE jobs SET last_error = last_error || jsonb_build_object("
                "'reconcile_after', CAST(:t AS TEXT)) WHERE id = :j"),
                {"j": jid, "t": (now_utc() - timedelta(seconds=1)).isoformat()})
        out = ops.reconcile_unknown_outcomes(eng, get_settings())
        assert out["still_unknown"] == 1
        state = _job_state(eng, jid)
        assert state["status"] == "failed_retryable"
        assert state["last_error"]["code"] == "unknown_outcome"
        assert state["run_after"] > now_utc() + timedelta(days=1)
    finally:
        _cleanup(eng, project)


def test_the_merge_reconciler_asks_the_repository_host(eng, project, monkeypatch):  # noqa: F811
    """The real reconciler, against a stubbed host: merged → happened; open → did not happen;
    unreadable → unknown. The host is the authority, never the job's own last words."""
    from boss_engine.services import software_factory as sf

    answers: dict[str, object] = {}

    class _Host:
        repository_full_name = "acme/app"

        def pull_request_state(self, number: int) -> dict:
            value = answers["state"]
            if isinstance(value, Exception):
                raise value
            return value  # type: ignore[return-value]

    class _Factory:
        def __init__(self, settings, *, owner=None, repo=None, **_kw):
            self.github = _Host()

        def bind_project_github(self, connection, project_id, **_kw):
            return {}

        _pull_request_settled = sf.SoftwareFactoryService._pull_request_settled

    monkeypatch.setattr(sf, "SoftwareFactoryService", _Factory)
    job_row = {"id": "job_x", "project_id": project, "kind": MERGE}
    payload = _merge_payload("dec_h")

    with eng.begin() as c:
        answers["state"] = {"merged": True, "merge_commit_sha": "feedface"}
        found, detail = ops._reconcile_pull_request_merge(c, get_settings(), job_row, payload)
        assert (found, detail["merged_sha"]) == ("happened", "feedface")

        answers["state"] = {"merged": False, "state": "open"}
        found, _detail = ops._reconcile_pull_request_merge(c, get_settings(), job_row, payload)
        assert found == "did_not_happen"

        answers["state"] = {"merged": False, "state": "closed"}
        found, detail = ops._reconcile_pull_request_merge(c, get_settings(), job_row, payload)
        assert (found, detail["pull_request_state"]) == ("did_not_happen", "closed")

        answers["state"] = RuntimeError("the host did not answer")
        found, _detail = ops._reconcile_pull_request_merge(c, get_settings(), job_row, payload)
        assert found == "unknown"

        found, _detail = ops._reconcile_pull_request_merge(
            c, get_settings(), job_row, {"pull_request_url": "not a url"})
        assert found == "unknown"


def test_a_confirmed_no_with_spent_attempts_ends_instead_of_looping(eng, project):  # noqa: F811
    """Release-then-park cycles are bounded: once attempts are spent, a confirmed
    'never happened' ends the job in words instead of releasing it again."""
    jid, rcpt = _park(eng, project, "dec_r4")
    try:
        with eng.begin() as c:
            c.execute(text("UPDATE jobs SET attempt = 3, max_attempts = 3 WHERE id = :j"),
                      {"j": jid})
        out = ops.reconcile_unknown_outcomes(
            eng, get_settings(),
            reconcilers={MERGE: lambda c, s, job, payload: ("did_not_happen",
                                                            {"pull_request_state": "open"})})
        assert out["resolved_did_not_happen"] == 1
        state = _job_state(eng, jid)
        assert state["status"] == "deadlettered"
        with eng.begin() as c:
            assert c.execute(text("SELECT status FROM receipts WHERE id = :r"),
                             {"r": rcpt}).scalar() == "failed"
            reason = c.execute(text("SELECT reason FROM deadletters WHERE source_id = :j"),
                               {"j": jid}).scalar()
        assert "never happened" in str(reason)
    finally:
        _cleanup(eng, project)
