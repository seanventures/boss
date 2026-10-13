"""A run interrupted after its pull request was opened could never complete.

`propose_pull_request` is deduped on `code:task:<run_id>`. When a run is re-delivered — a
deploy, a crash, a lost lease — the second attempt hits that dedupe key and returns
`status="reused"`, carrying the prior receipt's `pull_request_url` and `pull_request_number`.
That is the idempotency layer doing exactly its job.

The retry loop already knew this: it breaks out of the loop on "reused". Then the terminal check
two lines below rejected the very status the loop had accepted, and the run was blocked with
`PR not created after 3 attempts: reused` and a null pull_request_url — while the pull request
sat open on GitHub. Two conditions about one thing, disagreeing.

Measured on crun_1da493dc: PR created 23:50:46, run ended `blocked` an hour later, gates
`code_review_passed / security_clean / tests_strong_enough` all reported as failing on a change
that had already shipped.
"""

from __future__ import annotations

import inspect

from boss_engine.services import governed_code_task_service as g


def _terminal_check() -> str:
    src = inspect.getsource(g)
    start = src.index("AN ALREADY-OPEN PULL REQUEST IS A SUCCESS")
    return src[start:start + 3000]


def test_a_reused_proposal_is_accepted():
    body = _terminal_check()
    assert '_reused_pr = status == "reused"' in body
    assert 'if status not in ("created", "succeeded") and not _reused_pr:' in body


def test_it_only_counts_when_a_pull_request_actually_exists():
    """A reuse of some earlier effect that produced no PR is not a shipped change. Accepting
    `reused` unconditionally would mark such a run complete with a null URL — the same lie in
    the other direction."""
    body = _terminal_check()
    assert 'bool(proposal.get("pull_request_url"))' in body


def test_the_retry_loop_and_the_terminal_check_agree():
    """The bug was not either condition on its own — it was that they differed. Whatever the
    loop treats as done, the terminal check must too."""
    src = inspect.getsource(g)
    loop = src[src.index("for _pr_attempt in (2, 3):"):]
    loop_accepts = loop[:loop.index("with _transaction")]
    assert '"reused"' in loop_accepts, "the loop no longer breaks on reused"
    assert '"reused"' in _terminal_check(), "the terminal check no longer accepts reused"


def test_the_url_recorded_is_the_one_the_proposal_returned():
    """The reused branch carries the prior receipt's pull_request_url and number, so the run
    records the PR that actually exists rather than nothing."""
    sf = inspect.getsource(
        __import__("boss_engine.services.software_factory", fromlist=["x"])
    )
    # There are two "reused" returns in that module; only one is the PR path.
    pr_method = sf[sf.index("def propose_pull_request"):]
    reused = pr_method[pr_method.index('"status": "reused"'):]
    assert '"pull_request_url": result.get("pull_request_url")' in reused[:900]
    assert '"pull_request_number": result.get("pull_request_number")' in reused[:900]

    src = inspect.getsource(g)
    terminal = src[src.index('"phase=\'done\', pull_request_url=:u'):]
    assert 'proposal.get("pull_request_url")' in terminal[:500]
