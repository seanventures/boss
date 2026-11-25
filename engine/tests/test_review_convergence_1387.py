"""BOSSAPP-1387: Review convergence — pin met acceptance items on the same clean head_sha,
and label/demote newly noticed findings on unchanged lines under Standard/Prototype bars."""
from __future__ import annotations

from boss_engine.services import acceptance_gate, quality_bar as qb, review_tools
from boss_engine.services.governed_code_task_service import _review_gate_from_findings


def _make_northgate_tree(tmp_path):
    (tmp_path / "cmd" / "gateway").mkdir(parents=True)
    (tmp_path / "internal" / "proxy").mkdir(parents=True)
    (tmp_path / "go.mod").write_text("module northgate\n\ngo 1.22\n")
    (tmp_path / "cmd" / "gateway" / "main.go").write_text(
        "package main\n\nimport \"northgate/internal/proxy\"\n\nfunc main() { proxy.Start() }\n"
    )
    (tmp_path / "internal" / "proxy" / "server.go").write_text(
        "package proxy\n\nfunc Start() {}\nfunc Health() bool { return true }\n"
    )
    return tmp_path


def _northgate_items():
    return [
        {"n": 1, "id": "AC-1", "text": "Gateway entrypoint starts HTTP proxy"},
        {"n": 2, "id": "AC-5", "text": "Health endpoint returns ok"},
        {"n": 3, "id": "AC-8", "text": "Upstream proxy forwarding wired"},
        {"n": 4, "id": "AC-11", "text": "Timeout middleware configured"},
        {"n": 5, "id": "AC-21", "text": "Circuit breaker resets cleanly"},
        {"n": 6, "id": "AC-25", "text": "Metrics handler mounted"},
        {"n": 7, "id": "AC-29", "text": "Graceful shutdown drains connections"},
    ]


def test_northgate_replay_same_head_sha_keeps_met_set_stable(tmp_path):
    root = _make_northgate_tree(tmp_path)
    items = _northgate_items()
    stack = {"missing": [], "present": ["go.mod"]}
    head_sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

    # Run 1 (crun_159467cd13ec4a71b5e1 shape): all 7 items MET with valid file evidence.
    reply_1 = "\n".join([
        "ITEM 1: MET — cmd/gateway/main.go:5 starts proxy.Start()",
        "ITEM 2: MET — internal/proxy/server.go:4 Health() implemented",
        "ITEM 3: MET — cmd/gateway/main.go:3 imports internal/proxy",
        "ITEM 4: MET — internal/proxy/server.go:3 Start() sets server",
        "ITEM 5: MET — internal/proxy/server.go:3 Start() handles state",
        "ITEM 6: MET — internal/proxy/server.go:4 Health() exposes handler",
        "ITEM 7: MET — cmd/gateway/main.go:5 main() calls Start()",
        "ACCEPTANCE: MET",
    ])
    rec_1 = acceptance_gate.judge(
        items, reply_1, stack, root=root, head_sha=head_sha
    )
    assert rec_1["verdict"] == "met"
    assert rec_1["met"] == [1, 2, 3, 4, 5, 6, 7]

    # Run 2 (crun_ae6266628fbd4a47b45c shape) on the EXACT SAME head_sha:
    # Reviewer flips all 7 items to PARTIAL subjectively with no contradicting evidence.
    reply_2 = "\n".join([
        "ITEM 1: PARTIAL — cmd/gateway/main.go:5 could log startup banner",
        "ITEM 2: PARTIAL — internal/proxy/server.go:4 missing extra header",
        "ITEM 3: PARTIAL — cmd/gateway/main.go:3 style concern",
        "ITEM 4: PARTIAL — internal/proxy/server.go:3 default timeout",
        "ITEM 5: PARTIAL — internal/proxy/server.go:3 retry count",
        "ITEM 6: PARTIAL — internal/proxy/server.go:4 metrics format",
        "ITEM 7: PARTIAL — cmd/gateway/main.go:5 signal ordering",
        "ACCEPTANCE: NOT MET",
    ])
    rec_2 = acceptance_gate.judge(
        items, reply_2, stack, root=root, prior=rec_1, head_sha=head_sha
    )
    assert rec_2["verdict"] == "met"
    assert rec_2["met"] == [1, 2, 3, 4, 5, 6, 7]
    assert rec_2["partial"] == []
    assert all(i.get("pinned") is True for i in rec_2["items"])


def test_pinning_unpins_on_explicit_contradicting_evidence_and_skips_when_head_sha_missing(tmp_path):
    root = _make_northgate_tree(tmp_path)
    items = _northgate_items()[:2]
    stack = {"missing": [], "present": ["go.mod"]}
    head_sha = "deadbeef" * 5

    reply_1 = "\n".join([
        "ITEM 1: MET — cmd/gateway/main.go:5 starts proxy",
        "ITEM 2: MET — internal/proxy/server.go:4 Health() implemented",
        "ACCEPTANCE: MET",
    ])
    rec_1 = acceptance_gate.judge(items, reply_1, stack, root=root, head_sha=head_sha)
    assert rec_1["verdict"] == "met"

    # Explicit contradicting check failure on item 2 unpins item 2 while item 1 stays pinned.
    reply_2 = "\n".join([
        "ITEM 1: PARTIAL — minor nit",
        "ITEM 2: PARTIAL — `go test ./internal/proxy` failed",
        "ACCEPTANCE: NOT MET",
    ])
    rec_2 = acceptance_gate.judge(
        items,
        reply_2,
        stack,
        root=root,
        prior=rec_1,
        head_sha=head_sha,
        checks=[{"command": "go test ./internal/proxy", "ok": False, "item": "AC-5"}],
    )
    assert rec_2["met"] == [1]
    assert rec_2["partial"] == [2]
    assert rec_2["verdict"] == "not_met"

    # When head_sha is None (regression check across commits/tests), never pin.
    rec_no_sha = acceptance_gate.judge(
        items, reply_2, stack, root=root, prior=rec_1, head_sha=None
    )
    assert rec_no_sha["met"] == []
    assert rec_no_sha["partial"] == [1, 2]


def test_unchanged_line_finding_is_newly_noticed_and_demoted_under_standard_only():
    prev_files = {
        "src/app.py": "\n".join([
            "def first():",
            "    return 1",
            "",
            "def untouched():",
            "    return 42",
        ]),
    }
    # Fix round inserted 2 lines at the top of first(), shifting untouched() from line 4-5 to 6-7.
    curr_files = {
        "src/app.py": "\n".join([
            "def first():",
            "    # added validation",
            "    x = 1",
            "    return x",
            "",
            "def untouched():",
            "    return 42",
        ]),
    }
    prior_summary = "FINDING: BLOCKER src/app.py:2 — first() needs validation\nVERDICT: REFUTED"
    # New BLOCKER raised on shifted untouched line 7:
    new_on_unchanged = "FINDING: BLOCKER src/app.py:7 — untouched() returns magic constant\nVERDICT: REFUTED"
    ctx = {
        "prev_files": prev_files,
        "curr_files": curr_files,
        "prior_summary": prior_summary,
        "stage": "review_code",
    }

    # Standard bar: demoted to follow-up and labelled newly noticed!
    ok_std, blocking_std, deferred_std = _review_gate_from_findings(
        new_on_unchanged,
        "refuted",
        qb.review_block_severity(qb.STANDARD),
        unchanged_context=ctx,
        bar=qb.STANDARD,
    )
    assert ok_std is True
    assert blocking_std == []
    assert len(deferred_std) == 1
    assert deferred_std[0]["newly_noticed"] is True
    assert "newly noticed" in deferred_std[0]["text"].lower()

    items = qb.followup_items({"deferred_findings": deferred_std})
    assert items and items[0]["newly_noticed"] is True
    assert "newly noticed" in items[0]["text"].lower()

    # Production bar: still blocks even if newly noticed!
    ok_prod, blocking_prod, _ = _review_gate_from_findings(
        new_on_unchanged,
        "refuted",
        qb.review_block_severity(qb.PRODUCTION),
        unchanged_context=ctx,
        bar=qb.PRODUCTION,
    )
    assert ok_prod is False
    assert len(blocking_prod) == 1

    # Persisting finding from prior round still blocks under Standard!
    persisting = "FINDING: BLOCKER src/app.py:3 — first() needs validation\nVERDICT: REFUTED"
    ok_persist, blocking_persist, _ = _review_gate_from_findings(
        persisting,
        "refuted",
        qb.review_block_severity(qb.STANDARD),
        unchanged_context=ctx,
        bar=qb.STANDARD,
    )
    assert ok_persist is False
    assert len(blocking_persist) == 1

    # Finding on changed lines (line 3) still blocks under Standard!
    changed_line = "FINDING: BLOCKER src/app.py:3 — x is hardcoded\nVERDICT: REFUTED"
    ok_chg, blocking_chg, _ = _review_gate_from_findings(
        changed_line,
        "refuted",
        qb.review_block_severity(qb.STANDARD),
        unchanged_context=ctx,
        bar=qb.STANDARD,
    )
    assert ok_chg is False
    assert len(blocking_chg) == 1


def test_review_tools_hunk_helpers_and_path_resolution():
    prev_files = {"pkg/a.py": "one\ntwo\nthree\n", "pkg/b.py": "alpha\nbeta\n"}
    curr_files = {"pkg/a.py": "one\ntwo_mod\nthree\n", "pkg/b.py": "alpha\nbeta\n", "pkg/new.py": "hi\n"}
    hunks = review_tools.changed_hunks(prev_files, curr_files)
    assert hunks["pkg/a.py"] == [(2, 2)]
    assert hunks["pkg/b.py"] == []
    assert hunks["pkg/new.py"] is None

    assert review_tools.finding_touches_changed_lines({"path": "pkg/a.py", "line": 2}, hunks, curr_files) is True
    assert review_tools.finding_touches_changed_lines({"path": "pkg/a.py", "line": 3}, hunks, curr_files) is False
    assert review_tools.finding_touches_changed_lines({"path": "pkg/b.py", "line": 1}, hunks, curr_files) is False
    assert review_tools.finding_touches_changed_lines({"path": "pkg/new.py", "line": 1}, hunks, curr_files) is True
    # Unanchored finding fails closed (treated as changed)
    assert review_tools.finding_touches_changed_lines({"path": None, "line": None}, hunks, curr_files) is True
