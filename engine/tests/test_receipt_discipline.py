"""A receipt that is never closed is not a receipt.

Found by the v2 Receipts screen on its first run against live data: 1,428 of 1,606 receipts in
24 hours — 88% — sat at `in_progress` forever. Seven effect kinds were producing them and
exactly one, ai_sre.overwatch, ever called mark_succeeded.

The others create the receipt, do the work, emit an event saying the work happened, and return
without closing it. The work completed; the record says it never did.

WHY THIS MATTERS BEYOND TIDINESS
--------------------------------
A receipt exists to answer "did this effect actually happen?". If a successful effect and an
effect lost to a mid-flight restart both end up at `in_progress`, the receipt cannot answer it
— and that is the exact question the "the API restarted while this deployment was running"
investigation needed. 88% noise makes the signal unfindable.
"""

import ast
import pathlib

import pytest

SERVICES = pathlib.Path(__file__).resolve().parents[1] / "boss_engine" / "services"

#: Receipts that are legitimately still open when the creating function returns, because the
#: effect really does resolve later and elsewhere. Closing these would be a worse lie than
#: leaving them open.
DEFERRED_BY_DESIGN = {
    "receipts.py::create_or_reuse",           # the helper itself
    "receipts.py::record_completed",          # wraps the helper; closes via mark_succeeded
    "model_gateway.py::start_invocation",     # an invocation that finishes on the response
    "jobs.py::_defer_for_budget_state",       # deferred on purpose; that IS the state
    "jobs.py::_create_or_reuse_budget_policy_decision",   # resolves when a human answers
}

#: Known open create-and-forget sites, as a RATCHET rather than an exemption list.
#:
#: The v2 Receipts screen found 1,428 of 1,606 receipts in 24 hours sitting at in_progress
#: forever -- 88% -- because these create a receipt, do the work, emit an event saying the work
#: happened, and return without closing it. The six producing that live noise are fixed; the
#: rest are recorded here so the number can only go DOWN.
#:
#: A new offender fails the test. Removing one from this list is the fix.
KNOWN_OPEN = {
    "canaries.py::_canary_exactly_once",
    "canonical.py::_record_meeting_run_receipt",
    "code_task_service.py::_release",
    "commercial.py::_create_account_connect_receipt",
    "focused_agents.py::tick_one",
    "github_code_sync.py::_apply_resolution_branch",
    "github_code_sync.py::create_or_reuse_ref",
    "intent_router.py::_dispatch",
    "meeting_service.py::run_simulated_meeting",
    "metatron_captain.py::_apply_validated_move",
    "metatron_captain.py::_build_process",
    "orchestrator_next_move.py::execute",
}


def _functions_with_receipts() -> list[tuple[str, str, ast.FunctionDef]]:
    """Every function that creates a receipt, with its file and name."""
    out = []
    for path in sorted(SERVICES.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.dump(node)
            if "create_or_reuse" in body:
                out.append((path.name, node.name, node))
    return out


def _closes_its_receipt(node: ast.AST) -> bool:
    body = ast.dump(node)
    return "mark_succeeded" in body or "mark_failed" in body


def test_no_new_create_and_forget_appears():
    """The ratchet.

    An effect that finishes inside the function that started it must record that it finished.
    The existing offenders are listed in KNOWN_OPEN; anything NEW fails here.
    """
    offenders = {
        f"{filename}::{func}"
        for filename, func, node in _functions_with_receipts()
        if not _closes_its_receipt(node)
    } - DEFERRED_BY_DESIGN
    new = sorted(offenders - KNOWN_OPEN)
    assert not new, (
        "these create a receipt and never close it, so completed work is recorded as "
        f"permanently in progress: {new}"
    )


def test_the_known_list_does_not_rot():
    """An entry that has been fixed must be removed, or the ratchet stops ratcheting: a stale
    list would silently re-admit the same defect under the same name."""
    offenders = {
        f"{filename}::{func}"
        for filename, func, node in _functions_with_receipts()
        if not _closes_its_receipt(node)
    } - DEFERRED_BY_DESIGN
    stale = sorted(KNOWN_OPEN - offenders)
    assert not stale, f"already fixed, remove from KNOWN_OPEN: {stale}"


def test_the_six_live_offenders_are_actually_fixed():
    """Measured, not assumed. These seven effect kinds produced every receipt in the live
    24-hour window and exactly one of them -- ai_sre.overwatch -- ever closed its receipt."""
    fixed = {
        "process_manager.py::_ensure_receipt",
        "metatron_captain.py::watch_once",
        "metatron_captain.py::overwatch_once",
        # Eleven steps per process run, every one recorded as permanently in progress.
        "agent_process.py::_record_process_receipt",
    }
    open_now = {
        f"{filename}::{func}"
        for filename, func, node in _functions_with_receipts()
        if not _closes_its_receipt(node)
    }
    assert not (fixed & open_now), sorted(fixed & open_now)


def test_the_deferred_list_stays_small():
    """The easy way to make the ratchet pass is to call something deferred-by-design. Keeping
    that list short means each entry has to be a claim someone made on purpose."""
    assert len(DEFERRED_BY_DESIGN) <= 6


def test_there_is_something_to_check():
    """A test that silently finds no receipt sites would pass forever while proving nothing."""
    assert len(_functions_with_receipts()) >= 5


# --------------------------------------------------------------------------- #
# record_completed itself
# --------------------------------------------------------------------------- #
class _FakeConn:
    def __init__(self):
        self.updates = []

    def execute(self, statement, params=None):
        text_sql = str(statement)
        if "UPDATE receipts" in text_sql:
            self.updates.append(dict(params or {}))

        class _R:
            def mappings(self):
                return self

            def first(self):
                return None

            def one(self):
                return {}

        return _R()


def _service(created: bool):
    from boss_engine.services.receipts import ReceiptService

    svc = ReceiptService()
    svc.create_or_reuse = lambda *a, **k: ({"id": "rcp_1"}, created)  # type: ignore[method-assign]
    return svc


def test_record_completed_closes_the_receipt_it_writes():
    svc = _service(created=True)
    conn = _FakeConn()
    receipt, created = svc.record_completed(
        conn, "org", "ws", "proj", "dedupe", "thing.done", {}, {"ok": True})
    assert created is True
    assert receipt["status"] == "succeeded"
    assert len(conn.updates) == 1


def test_a_reused_receipt_is_not_re_marked():
    """Re-marking a deduped receipt would overwrite the result of the effect that actually ran
    with the payload of the one that was deduped away."""
    svc = _service(created=False)
    conn = _FakeConn()
    receipt, created = svc.record_completed(
        conn, "org", "ws", "proj", "dedupe", "thing.done", {}, {"ok": True})
    assert created is False
    assert conn.updates == []
    assert "status" not in receipt or receipt.get("status") != "succeeded"
