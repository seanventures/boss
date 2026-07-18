"""VAG #107 — cross-run eval ratchet: capturing human↔gate disagreements as growing corpus
items and merging them into the #105 meta-eval. Pure helpers need no DB; tag/load/merge run
against the isolated boss_test DB via the engine fixture."""
import json

from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.app.services import verifier_ratchet as vr
from apps.api.app.services.core import make_id


# --- pure helpers ------------------------------------------------------------
def test_files_from_prefers_explicit_then_pending_diff():
    run = {"finalize_ctx": {"pending_diff": [{"path": "a.py", "content": "x"}]}}
    assert vr._files_from(run, None) == [{"path": "a.py", "content": "x"}]
    # explicit wins
    assert vr._files_from(run, [{"path": "b.py", "content": "y"}]) == [{"path": "b.py", "content": "y"}]
    # nothing recoverable
    assert vr._files_from({"finalize_ctx": {}}, None) is None
    assert vr._files_from({"finalize_ctx": {"pending_diff": []}}, []) is None


def test_item_from_run_builds_corpus_item_with_ground_truth_stub():
    run = {
        "id": "crun_abc123", "title": "Do the thing",
        "spec": {"acceptance_criteria": ["c1", "c2"], "instructions": "impl X"},
        "finalize_ctx": {"ratchet": {
            "label": "bugged", "source": "decision_reject", "bug_class": "human-rejected-false-pass",
            "files": [{"path": "solution.py", "content": "def f(): pass"}], "reason": "wrong output",
        }},
    }
    item = vr.item_from_run(run)
    assert item["label"] == "bugged" and item["stub_verdict"] == "refuted"  # bugged -> perfect verifier refutes
    assert item["implementation"] == {"solution.py": "def f(): pass"}
    assert item["acceptance_criteria"] == ["c1", "c2"]
    assert item["provenance"]["run_id"] == "crun_abc123"
    assert item["key"].startswith("ratchet_")


def test_item_from_run_clean_uses_survived_stub_and_none_when_untagged():
    clean = {"id": "r1", "spec": {}, "finalize_ctx": {"ratchet": {
        "label": "clean", "source": "gate_override", "files": [{"path": "a.py", "content": "ok"}]}}}
    assert vr.item_from_run(clean)["stub_verdict"] == "survived"
    assert vr.item_from_run({"finalize_ctx": {}}) is None            # untagged
    assert vr.item_from_run({"finalize_ctx": {"ratchet": {"label": "clean", "files": []}}}) is None  # fileless


# --- DB-backed (boss_test) ---------------------------------------------------
def _insert_run(connection, *, project_id, pending_diff=None, title="Ratchet test"):
    run_id = make_id("crun")
    fctx = {"pending_diff": pending_diff} if pending_diff else {}
    connection.execute(
        text("INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, finalize_ctx) "
             "VALUES (:id, :p, NULL, 'completed', 'done', :t, CAST(:spec AS JSONB), CAST(:fc AS JSONB))"),
        {"id": run_id, "p": project_id, "t": title,
         "spec": json.dumps({"acceptance_criteria": ["works"], "instructions": "do it"}),
         "fc": json.dumps(fctx)},
    )
    return run_id


def _a_project(connection):
    # any seeded project id; the ratchet doesn't care which
    return connection.execute(text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar_one()


def test_tag_load_and_merge_roundtrip(engine):
    with begin(engine) as c:
        pid = _a_project(c)
        run_id = _insert_run(c, project_id=pid,
                             pending_diff=[{"path": "solution.py", "content": "def f(): return 0\n"}])
        # first capture writes the tag
        assert vr.tag_run(c, run_id, label="clean", source="gate_override") is True
        # idempotent on source
        assert vr.tag_run(c, run_id, label="clean", source="gate_override") is False

    with begin(engine) as c:
        items = vr.load_ratchet_items(c, pid)
        keys = [i["key"] for i in items]
        assert f"ratchet_{run_id[:16]}" in keys
        it = next(i for i in items if i["key"] == f"ratchet_{run_id[:16]}")
        assert it["label"] == "clean" and it["implementation"]["solution.py"].startswith("def f()")

        base = {"items": [{"key": "seed1", "label": "bugged"}]}
        merged = vr.merged_corpus(c, base, pid)
        assert merged["ratchet_count"] >= 1
        assert any(i["key"] == "seed1" for i in merged["items"])           # base preserved
        assert any(i["key"].startswith("ratchet_") for i in merged["items"])  # ratchet folded in


def test_tag_run_without_files_is_noop(engine):
    with begin(engine) as c:
        pid = _a_project(c)
        run_id = _insert_run(c, project_id=pid, pending_diff=None)   # gate-passed run: no preserved diff
        assert vr.tag_run(c, run_id, label="bugged", source="decision_reject") is False
        assert vr.load_ratchet_items(c, pid) == []


def test_capture_run_with_explicit_files(engine):
    with begin(engine) as c:
        pid = _a_project(c)
        run_id = _insert_run(c, project_id=pid, pending_diff=None)
        res = vr.capture_run(c, run_id, label="bugged", source="manual",
                             files=[{"path": "bug.py", "content": "def g(): return 1/0\n"}],
                             bug_class="div-by-zero", reason="crashes")
        assert res["captured"] is True
    with begin(engine) as c:
        items = vr.load_ratchet_items(c, pid)
        assert items and items[0]["label"] == "bugged"
        assert items[0]["bug_class"] == "div-by-zero"
        assert items[0]["stub_verdict"] == "refuted"


# --- endpoints ---------------------------------------------------------------
def test_capture_endpoint_validates_and_meta_eval_includes_ratchet(client, engine):
    with begin(engine) as c:
        pid = _a_project(c)
        run_id = _insert_run(c, project_id=pid, pending_diff=None)

    # bad label rejected
    assert client.post("/v1/verifier-ratchet/capture", json={"run_id": run_id, "label": "nope"}).status_code == 422

    # capture a bugged item with explicit files
    resp = client.post("/v1/verifier-ratchet/capture", json={
        "run_id": run_id, "label": "bugged",
        "files": [{"path": "s.py", "content": "def f(): return None\n"}],
    })
    assert resp.status_code == 200 and resp.json()["captured"] is True

    # the meta-eval dry-run now includes the ratchet item (still gates PASS: stub is ground truth)
    body = client.post("/v1/verifier-meta-eval",
                       json={"dry_run": True, "project_id": pid}).json()
    assert any(i["key"].startswith("ratchet_") for i in body["items"])
    assert body["gate"]["passed"] is True
