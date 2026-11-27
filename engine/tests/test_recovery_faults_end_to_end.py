"""THE RECOVERY SCENARIOS, END TO END, WITH NO MODEL (test plan section 11, ENT-REC-*).

The faults are the fault kit's own (scripts/install/qa_fault.py: what `boss qa fault` pushes for
the tester), applied to the plain Node sample, and the build is BOSS's REAL governed pipeline on
the scripted conformance engine (the harness of test_the_pipeline_runs_end_to_end.py: the bench
database, a local GitHub, builds started as the API starts them and run as the worker runs them).
Every run the build spawns — its repair rounds, its follow-up — is run too, until nothing is
queued: a LINEAGE, the plan's "attempt at the case".

Two shapes of fault:
  * IN THE BASE (the plan's Way 1): the fault is merged before the New work starts, in a file the
    build does not write (REC-01: an existing test of the app is broken);
  * STICKY: the scripted coder writes the fault on every writing turn, so no repair can clear it
    (REC-04, REC-09) — the case where BOSS "cannot fix it" and must say so.

What is asserted is the plan's BOSS-side behaviour that needs no model's intelligence:
  * the failure is detected and NAMED (the failing test, the missing package, the app's exit);
  * repair rounds are BOUNDED and VISIBLE (each round an event with its number and its limit, a
    time; the ledger reads attempts and times back — services/qa_ledger.py);
  * when the repairs are spent, a DRAFT change marked checks failing opens WITH a follow-up, and
    the lineage ENDS (no loop: nothing left queued, a bounded number of builds);
  * the same cause twice ESCALATES to a person, never the same action again (ENT-REC-52);
  * every repair, retry and follow-up stays on the FAILING APP of a two-app project (ENT-REC-51).

Where the product parts from the plan the test is xfail(strict) with the case and the reason (the
PR lists each as a product bug); it fails loudly the day the product changes.

A PERSON STARTED THIS WORK. The harness enters builds without a person (identity in shadow), so
BOSS's automatic repair would find no grant to carry and stop ("no person's grant covers another
attempt"); in the plan a person pressed New work. `carry_retry_authority` stands in for that
person's grant here — the only seam replaced.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "install"))
import qa_fault  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "pipeline_harness", Path(__file__).with_name("test_the_pipeline_runs_end_to_end.py"))
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

FIXTURE = harness.FIXTURE
FAILING_TEST = "slug lowercases and joins words with hyphens"
#: The plan's cap for the same cause (ENT-REC-52: "at most 3 attempts in total"): BOSS's one
#: cap per lineage (services/lineage_cap.py) across the build, its repairs and follow-ups.
PLAN_ATTEMPTS = 3
#: The most builds one attempt at a case may take.
MAX_BUILDS = PLAN_ATTEMPTS

pytestmark = pytest.mark.skipif(not os.environ.get("BOSS_TEST_DATABASE_URL"),
                                reason="needs the bench database (BOSS_TEST_DATABASE_URL)")


class _NoModel:
    """The decision seat with no model: every typed decision falls back to BOSS's own rule."""

    def decide(self, *_a, **_k):
        raise RuntimeError("no model in this test")


@pytest.fixture(scope="module")
def pipe(tmp_path_factory):
    saved = {k: os.environ.get(k) for k in harness._ENV}
    os.environ["DATABASE_URL"] = os.environ["BOSS_TEST_DATABASE_URL"]
    os.environ["BOSS_AUTHZ_MODE"] = "shadow"
    os.environ["BOSS_SANDBOX_CI_TIMEOUT_SECONDS"] = "1"
    os.environ["BOSS_SANDBOX_CI_POLL_INTERVAL_SECONDS"] = "1"
    from boss_engine.config import get_settings
    from boss_engine.identity import consent, lineage
    from boss_engine.services import playwright_cache, typed_decisions

    get_settings.cache_clear()
    mp = pytest.MonkeyPatch()
    # NO MODEL DECIDES EITHER. The typed decision seat (the risk tier, plan coverage) is not an
    # engine: left alone it reached the laptop's real Claude CLI, and where a lineage parked
    # (a follow-up waiting on a plan approval, or not) varied with what that model answered.
    mp.setattr(typed_decisions, "make_decider", lambda *_a, **_k: _NoModel())
    mp.setattr(playwright_cache, "ensure", lambda *_a, **_k: {
        "ok": True, "cached": True, "seconds": 0.0, "path": None,
        "detail": "harness: the browser cache is warm"})
    # The person who pressed New work: their grant covers BOSS's repairs of it (see docstring).
    mp.setattr(consent, "carry_retry_authority", lambda db, **kw: lineage.Acting(
        principal_id="usr_recovery_tester", org_id="org_pipe", run_id=kw.get("new_run_id")))
    p = harness.Pipeline(tmp_path_factory.mktemp("recovery"))
    p.work = tmp_path_factory.mktemp("faults")
    try:
        yield p
    finally:
        mp.undo()
        p.close()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


# ── one attempt at a case ────────────────────────────────────────────────────────────────────

def _faulted(pipe, case: str, *, app: str = "") -> Path:
    """The plain Node sample with the kit's fault for `case` applied."""
    out = pipe.work / f"{case}-{uuid.uuid4().hex[:6]}"
    shutil.copytree(FIXTURE, out)
    qa_fault.apply(case, out)
    return out


def _tree(root: Path, prefix: str = "") -> dict[str, str]:
    return {prefix + p.relative_to(root).as_posix(): p.read_text()
            for p in root.rglob("*") if p.is_file()}


def _new_work_fixture(pipe) -> Path:
    """What the New work writes on an app whose existing test is broken: its own new tests and
    the app, never the existing test file (a test it does not own)."""
    out = pipe.work / f"new-work-{uuid.uuid4().hex[:6]}"
    shutil.copytree(FIXTURE, out)
    (out / "test" / "slug.test.js").rename(out / "test" / "slug-more.test.js")
    return out


def start(pipe, *, tag: str, seed: dict[str, str], fixture: Path, app_dir: Optional[str] = None,
          brief_extra: str = "", test_command: str = "npm test") -> tuple[str, str]:
    """A build as the API starts one, on a repository seeded with `seed`; the scripted coder
    writes `fixture` (into `app_dir`, or where BOSS's prompt scopes it when app_dir is None)."""
    from boss_engine.services import code_task_service

    pipe._engine_env("")
    os.environ["BOSS_SCRIPTED_FIXTURE"] = str(fixture)
    if app_dir is None:
        os.environ.pop("BOSS_SCRIPTED_APP_DIR", None)   # follow BOSS's "Work ONLY under" scope
    else:
        os.environ["BOSS_SCRIPTED_APP_DIR"] = app_dir
    repo = f"rec-{tag}-{uuid.uuid4().hex[:6]}"
    pipe.repos[repo] = pipe.hub.seed(harness.OWNER, repo, {"README.md": f"# {repo}\n", **seed})
    with pipe.engine.begin() as c:
        run_id = code_task_service.start_code_task(
            c, project_id=pipe.project, task_id=None, title=f"Add a recipe counter ({tag})",
            instructions=("Add a recipe counter to the slug service: slug(text) in src/slug.js "
                          "and the HTTP server in src/server.js answering GET /?q=<text> with "
                          "{\"slug\": ...} on $PORT. Tests under node --test." + brief_extra),
            acceptance_criteria=["slug lowercases and joins words with hyphens",
                                 "GET /?q= answers the slug as JSON"],
            test_commands=[test_command], owner=harness.OWNER, repo=repo, base_branch="main",
            finalize_ctx={"app_name": "slug"}, jobs=pipe.jobs(repo),
            model_config={"default": "scripted"}, open_pr=True, allow_duplicate=True,
            profile="balanced", risk_tier=1)
    return run_id, repo


def run_lineage(pipe, root: str, repo: str, *, limit: int = 8) -> dict[str, Any]:
    """Run the build and everything it queues (repairs, follow-ups) until nothing is queued.
    More than `limit` builds is a loop: the test says so instead of running forever."""
    from sqlalchemy import text

    done: list[str] = []
    errors: dict[str, Optional[str]] = {}
    while True:
        with pipe.engine.begin() as c:
            queued = [r[0] for r in c.execute(text(
                "SELECT id FROM code_task_runs WHERE project_id = :p "
                "AND status IN ('queued', 'running') AND spec->>'repo' = :r "
                "ORDER BY created_at"), {"p": pipe.project, "r": repo})]
        nxt = root if not done else next((q for q in queued if q not in done), None)
        if nxt is None:
            break
        assert len(done) < limit, f"a loop: more than {limit} builds for one fault ({done})"
        errors[nxt] = pipe.run(nxt, repo)
        done.append(nxt)
    return collect(pipe, repo, done, errors)


def collect(pipe, repo: str, ran: list[str], errors: dict) -> dict[str, Any]:
    from sqlalchemy import text

    from boss_engine.services import scripted_engine

    with pipe.engine.begin() as c:
        runs = [dict(r) for r in c.execute(text(
            "SELECT * FROM code_task_runs WHERE project_id = :p AND spec->>'repo' = :r "
            "ORDER BY created_at"), {"p": pipe.project, "r": repo}).mappings()]
        ids = [r["id"] for r in runs]
        events = [dict(e) for e in c.execute(text(
            "SELECT entity_id, event_type, data, created_at FROM events WHERE entity_id = ANY(:i) "
            "ORDER BY created_at, id"), {"i": ids}).mappings()]
        gates = [dict(g) for g in c.execute(text(
            "SELECT code_task_run_id, gates_failed, evidence FROM quality_gates "
            "WHERE code_task_run_id = ANY(:i)"), {"i": ids}).mappings()]
        # The harness runs builds itself, so a build's job row stays queued: what is left to run
        # is a RUN that is still queued or running.
        queued = sum(1 for r in runs if r["status"] in ("queued", "running"))
    writes = {rid: [x for x in scripted_engine.run_state(rid).calls
                    if x["stage"] in ("write_tests", "implement", "solo", "repair", "quality")
                    and not x["read_only"]] for rid in ids}
    prs = [pr for pr in pipe.hub.world.prs if repo in json.dumps(pr, default=str)]
    return {"repo": repo, "runs": runs, "ran": ran, "errors": errors, "events": events,
            "gates": gates, "queued_jobs": queued, "writes": writes, "prs": prs,
            "labels": [x for x in pipe.hub.world.pr_labels if repo in json.dumps(x, default=str)]}


def _of(lin: dict, kind: str) -> list[dict]:
    return [e for e in lin["events"] if e["event_type"] == kind]


def _text(lin: dict) -> str:
    """Everything BOSS recorded about the lineage that a person or a repair round reads."""
    parts = [json.dumps({k: r.get(k) for k in ("error", "state", "finalize_ctx")}, default=str)
             for r in lin["runs"]]
    parts += [json.dumps(e["data"], default=str) for e in lin["events"]]
    parts += [json.dumps(g["evidence"], default=str) for g in lin["gates"]]
    parts += [str(pr.get("body") or "") + str(pr.get("title") or "") for pr in lin["prs"]]
    return "\n".join(parts)


def _actors(pipe, lin: dict) -> list:
    from sqlalchemy import text

    from boss_engine.services import qa_ledger

    with pipe.engine.begin() as c:
        rows = c.execute(text("SELECT DISTINCT actor, event_type FROM events "
                              "WHERE entity_id = ANY(:i)"), {"i": [r["id"] for r in lin["runs"]]})
        return sorted({(a, k) for a, k in rows if qa_ledger._is_person(c, pipe.project, a)})


def _diff(pipe, repo: str, branch: str) -> list[str]:
    bare = pipe.repos[repo]
    out = subprocess.run(["git", "--git-dir", str(bare), "diff", "--name-only", "main", branch],
                         capture_output=True, text=True, check=True).stdout
    return [x for x in out.splitlines() if x.strip()]


# ── the lineages (built once per module) ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def rec01(pipe):
    """ENT-REC-01, Way 1: the existing test is broken in the base; New work builds on it."""
    run_id, repo = start(pipe, tag="rec01", seed=_tree(_faulted(pipe, "REC-01")),
                         fixture=_new_work_fixture(pipe), app_dir="")
    return {**run_lineage(pipe, run_id, repo), "root": run_id}


@pytest.fixture(scope="module")
def rec09(pipe):
    """ENT-REC-09, sticky: the server the coder writes always throws on start."""
    run_id, repo = start(pipe, tag="rec09", seed={}, fixture=_faulted(pipe, "REC-09"), app_dir="")
    return {**run_lineage(pipe, run_id, repo), "root": run_id}


@pytest.fixture(scope="module")
def rec04(pipe):
    """ENT-REC-04, sticky: slug.js imports a package package.json never lists."""
    run_id, repo = start(pipe, tag="rec04", seed={}, fixture=_faulted(pipe, "REC-04"), app_dir="")
    return {**run_lineage(pipe, run_id, repo), "root": run_id}


# ── ENT-REC-01: detected, named, bounded, visible, a draft with a follow-up, no loop ─────────

def test_rec01_the_broken_existing_test_is_detected_and_named(rec01):
    first = rec01["runs"][0]
    failed = [g for g in rec01["gates"] if g["code_task_run_id"] == first["id"]]
    assert any("written_tests_pass" in (g["gates_failed"] or []) for g in failed), failed
    assert FAILING_TEST in _text(rec01), "the failing test is named where a person reads it"


def test_rec01_repair_rounds_are_bounded_and_each_is_visible(rec01):
    rounds = _of(rec01, "code_task.inrun_convergence_round") + \
        _of(rec01, "code_task.review_repair_round")
    assert rounds, "a repair round ran"
    for e in _of(rec01, "code_task.inrun_convergence_round"):
        d = e["data"]
        assert 1 <= int(d["round"]) <= int(d["of"]) and d["failing_gates"], d
        assert e["created_at"] is not None
    assert len(rec01["ran"]) <= MAX_BUILDS, rec01["ran"]
    assert sum(len(w) for w in rec01["writes"].values()) <= 4 * MAX_BUILDS


def test_rec01_a_draft_change_marked_checks_failing_opens_with_a_follow_up(rec01):
    opened = _of(rec01, "code_task.pr_opened_with_checks_failing")
    assert opened, "the change opens with its checks failing once the repairs are spent"
    assert "written_tests_pass" in opened[0]["data"]["failing_gates"]
    followups = [e for e in _of(rec01, "code_task.deferred_findings_task")
                 if (e["data"] or {}).get("followup_run_id")]
    assert followups, "a follow-up is queued for what is left"
    drafts = [pr for pr in rec01["prs"] if pr.get("draft")]
    assert drafts, "a DRAFT change, never one that reads ready to merge"
    assert any("checks failing" in json.dumps(x) for x in rec01["labels"]) or \
        any("checks failing" in str(pr.get("body") or "").lower() for pr in drafts)


def test_rec01_the_lineage_ends_no_loop(rec01):
    assert rec01["queued_jobs"] == 0
    assert all(r["status"] not in ("queued", "running") for r in rec01["runs"])
    assert len(rec01["runs"]) == len(rec01["ran"]) <= MAX_BUILDS


def test_rec52_the_same_cause_gets_at_most_three_builds(rec01):
    """The plan's cap, one for the lineage: the build, its repair and the checks-failing
    follow-up — never the follow-up's own repair (it used to restart the count at 1)."""
    same = [r["id"] for r in rec01["runs"] if any(
        "written_tests_pass" in (g["gates_failed"] or [])
        for g in rec01["gates"] if g["code_task_run_id"] == r["id"])]
    assert len(same) == len(rec01["runs"]) == PLAN_ATTEMPTS, [r["id"] for r in rec01["runs"]]


def test_rec52_the_lineage_parks_the_same_way_every_time(rec01):
    """Deterministic: the follow-up keeps the lineage's risk tier (BOSS's own finding text says
    "auth" and re-deciding the tier from it parked the follow-up on a plan approval, as the
    decider happened to answer), so it never waits on a plan approval; it ends as a draft."""
    followup = next(r for r in rec01["runs"] if (r["finalize_ctx"] or {}).get("is_followup"))
    assert (followup["spec"] or {}).get("risk_tier") == 1
    assert not [r for r in rec01["runs"] if r["phase"] == "awaiting_plan_approval"]
    assert rec01["runs"][-1]["id"] == followup["id"] and followup["status"] == "completed"


def test_rec52_at_the_cap_a_person_decides_with_a_next_step(rec01):
    """The third build is the last BOSS makes by itself: no repair child, no follow-up, and the
    draft's card says BOSS already tried that for this same cause and what a person does next."""
    from boss_engine.services import lineage_cap

    spent = _of(rec01, "code_task.lineage_attempts_spent")
    assert len(spent) == 1 and spent[0]["entity_id"] == rec01["runs"][-1]["id"], spent
    assert spent[0]["data"]["attempts"] == lineage_cap.CAP
    stopped = (rec01["runs"][-1]["finalize_ctx"]["checks_failing"] or {}).get("stopped") or {}
    assert stopped.get("reason") == lineage_cap.REASON and stopped.get("no_followup")
    assert "BOSS already tried that for this same cause" in stopped["words"]
    assert lineage_cap.NEXT_STEP in stopped["words"]


def test_rec01_the_ledger_reads_attempts_and_times_back(pipe, rec01):
    """WORK-04 / the tester's ledger: attempts, time and who acted, from BOSS's own records."""
    from boss_engine.services import qa_ledger

    with pipe.engine.begin() as c:
        rows = qa_ledger.ledger(c, pipe.project, case="REC-01")["rows"]
    row = next(r for r in rows if rec01["root"] in r["runs"])
    assert set(row["runs"]) == {r["id"] for r in rec01["runs"]}
    assert row["failure_at"] and row["draft_at"] and row["draft_at"] >= row["failure_at"]
    assert row["unattended"] is True and row["human_actions"] == 0, _actors(pipe, rec01)
    assert "build repair" in row["acted"] or "review repair" in row["acted"]
    # It ends as a draft, or parked on a person's card (a follow-up's plan approval): never
    # running, never queued.
    assert row["final_state"].startswith(("draft change", "completed", "failed", "blocked"))


# ── ENT-REC-52: the same cause twice escalates to a person ───────────────────────────────────

def _attempts(pipe, lin: dict) -> list[dict]:
    """Each build of the lineage that stopped on its checks, as the recovery agent reads it."""
    from boss_engine.services import step_recovery as sr

    failing = {}
    for g in lin["gates"]:
        if g["gates_failed"]:
            failing.setdefault(g["code_task_run_id"], sorted(g["gates_failed"]))
    out = []
    with pipe.engine.begin() as c:
        for rid in (r["id"] for r in lin["runs"]):
            if rid in failing:
                ev = sr.build_evidence(c, rid, "the build stopped on its checks")["failure"]
                out.append({**ev, "phase": sr.GATE_WAIT_PHASE, "failing_gates": failing[rid]})
    return out


def test_rec52_every_attempt_at_the_same_failing_test_has_one_cause_signature(pipe, rec01):
    from boss_engine.services import step_recovery as sr

    attempts = _attempts(pipe, rec01)
    assert len(attempts) >= 2, "the cause came back at least once"
    sigs = {sr.cause_signature(sr.FINDINGS_KIND, f)[0] for f in attempts}
    assert sigs == {"build:open_findings:written_tests_pass"}, sigs
    # ...although the gates beside it differ between attempts (a round that wrote nothing).
    assert len({tuple(f["failing_gates"]) for f in attempts}) > 1, attempts


def test_rec52_the_same_cause_twice_is_one_question_never_the_same_action(pipe, rec01):
    """The per-cause rule on the lineage's REAL first stop: a second decision on the same cause
    is one question for a person, and a model may not repeat the action either."""
    from boss_engine.services import step_recovery as sr

    failure = _attempts(pipe, rec01)[0]
    sig, pattern = sr.cause_signature(sr.FINDINGS_KIND, failure)
    allowed = sr.KINDS[sr.FINDINGS_KIND]
    first = sr.coded_decision(sr.FINDINGS_KIND, {"failure": failure}, signature=sig,
                              pattern=pattern, last={}, allowed=allowed)
    assert first["action"] == sr.FIX_APP
    last = {"signature": sig, "action": first["action"]}
    again = sr.coded_decision(sr.FINDINGS_KIND, {"failure": failure}, signature=sig,
                              pattern=pattern, last=last, allowed=allowed)
    assert again["action"] == sr.ASK_PERSON and again["question"].count("?") == 1
    decision, why = sr.validate({"action": first["action"], "note": "fix it",
                                 "evidence": ["the build stopped on its checks"]},
                                allowed=allowed, evidence={"failure": failure},
                                corpus=json.dumps(failure), last=last, signature=sig)
    assert decision is None and "same cause" in why


# ── ENT-REC-09 / REC-04, sticky: named, and never shipped as green ───────────────────────────

def test_rec09_the_crash_on_start_is_named_as_an_exit_before_serving(rec09):
    serve = [e["data"] for e in _of(rec09, "code_task.app_serve_checked")]
    assert serve and serve[0]["ok"] is False
    assert serve[0]["error"].startswith("the app exited (code 1) before serving HTTP")
    assert "boom: BOSS fault REC-09" in serve[0]["error"]
    rounds = _of(rec09, "code_task.inrun_convergence_round")
    assert rounds and "app_serves" in rounds[0]["data"]["failing_gates"]


@pytest.mark.xfail(strict=True, reason=(
    "ENT-REC-09: a repair round that writes nothing runnable skips the app-start proof "
    "(prove_app_serves: source 'no_runnable_change', ok), so the repair build of an app that "
    "still crashes on start completes green with an ordinary pull request — no draft, no "
    "follow-up"))
def test_rec09_an_app_that_still_crashes_never_completes_green(rec09):
    last = rec09["runs"][-1]
    green = last["status"] == "completed" and not _of(rec09, "code_task.pr_opened_with_checks_failing")
    assert not green, "the server still throws on start, so the lineage cannot end green"


def test_rec09_the_lineage_ends(rec09):
    assert rec09["queued_jobs"] == 0 and len(rec09["ran"]) <= MAX_BUILDS


def test_rec04_the_missing_package_is_named(rec04):
    assert "dayjs" in _text(rec04), "the package the app imports and does not list is named"
    assert _of(rec04, "code_task.inrun_convergence_round") or \
        _of(rec04, "code_task.review_repair_round")


def test_rec04_ends_in_a_draft_with_a_follow_up_not_a_loop(rec04):
    assert _of(rec04, "code_task.pr_opened_with_checks_failing")
    assert rec04["queued_jobs"] == 0 and len(rec04["ran"]) <= MAX_BUILDS


# ── ENT-REC-51: repairs, retries and follow-ups stay on the failing app ──────────────────────

@pytest.fixture(scope="module")
def rec51(pipe):
    """Two apps in one project; Recipe box's existing test is broken; New work on Recipe box."""
    broken = _tree(_faulted(pipe, "REC-01"), "recipe-box/")
    other = _tree(FIXTURE, "neighborhood-board/")
    # The scripted coder writes where the brief says, as a model reading it would; what is
    # asserted is what BOSS itself does with the lineage (its gates, repairs, follow-ups, Retry).
    run_id, repo = start(pipe, tag="rec51", seed={**broken, **other},
                         fixture=_new_work_fixture(pipe), app_dir="recipe-box",
                         brief_extra=" Build it in the `recipe-box/` folder of this repository.",
                         test_command="npm --prefix recipe-box test")
    lin = run_lineage(pipe, run_id, repo)
    # A PERSON presses Retry on the last build of the lineage (the Work panel's Retry).
    from boss_engine.services import code_task_service

    last = lin["runs"][-1]["id"]
    retried = code_task_service.retry_code_task(
        pipe.engine, pipe.settings(repo), pipe.jobs(repo), last, by_person=True,
        fix_note="Retry from the Work panel: the recipe counter still fails its checks.")
    retry_id = (retried or {}).get("run_id")
    if retry_id:
        lin = run_lineage(pipe, retry_id, repo, limit=12) | {"retry": retry_id}
    return {**lin, "root": run_id}


def test_rec51_every_build_of_the_lineage_writes_only_the_failing_app(pipe, rec51):
    branches = sorted(set(subprocess.run(
        ["git", "--git-dir", str(pipe.repos[rec51["repo"]]), "for-each-ref",
         "--format=%(refname:short)", "refs/heads"], capture_output=True, text=True,
        check=True).stdout.split()) - {"main"})
    assert branches, "the lineage pushed its work"
    for branch in branches:
        touched = _diff(pipe, rec51["repo"], branch)
        # (`F-<n>` at the root is the scripted engine's own brief-item note on a follow-up.)
        strays = [f for f in touched if not f.startswith("recipe-box/")
                  and not re.fullmatch(r"F-\d+", f)]
        assert not strays, f"{branch} changed files outside recipe-box/: {strays}"


def test_rec51_a_persons_retry_and_the_follow_ups_stay_on_the_failing_app(pipe, rec51):
    """Every build of the lineage — repair, follow-up, the person's Retry — is about Recipe box
    by BOSS's own reading of it (the brief's folder, else the app the lineage recorded)."""
    from boss_engine.services import app_root, verification_target

    assert rec51.get("retry"), "the person's Retry started a build"
    ids = [r["id"] for r in rec51["runs"]]
    assert rec51["retry"] in ids and len(ids) >= 2
    with pipe.engine.begin() as c:
        for run in rec51["runs"]:
            fctx = run.get("finalize_ctx") or {}
            app = (verification_target.app_scope(run.get("spec"), fctx)
                   or app_root.recorded_app_dir(c, run))
            assert app == "recipe-box", f"{run['id']} builds on {app!r}, not recipe-box/"


def test_rec51_every_writing_turn_is_told_to_stay_inside_the_failing_app(rec51):
    turns = [(rid, x["stage"]) for rid, r in rec51["writes"].items() for x in r]
    unscoped = [(rid, x["stage"]) for rid, r in rec51["writes"].items() for x in r
                if "Work ONLY under `recipe-box/`" not in x["full_prompt"]]
    assert turns and not unscoped, f"writing turns not scoped to recipe-box/: {unscoped}"
    # ...on every kind of build of the lineage: the build, a repair, the follow-up, the Retry.
    kinds = {}
    for run in rec51["runs"]:
        fctx = run.get("finalize_ctx") or {}
        kind = ("build" if run["id"] == rec51["root"] else "retry" if run["id"] == rec51["retry"]
                else "follow-up" if fctx.get("is_followup") and not fctx.get("attempt")
                else "repair")
        if rec51["writes"].get(run["id"]):
            kinds.setdefault(kind, []).append(run["id"])
    assert set(kinds) == {"build", "repair", "follow-up", "retry"}, kinds


def test_rec51_every_build_of_the_lineage_passes_the_stays_in_its_app_gate(rec51):
    """`stayed_in_app` is judged on every build of the lineage, and none changed another app."""
    judged = [g for g in rec51["gates"] if (g["evidence"] or {}).get("app_scope")]
    assert len(judged) == len(rec51["gates"]) >= 2
    for g in judged:
        assert g["evidence"]["app_scope"] == {"app": "recipe-box", "other_app_files": []}
        assert "stayed_in_app" not in (g["gates_failed"] or [])



@pytest.fixture(scope="module")
def rec51_stray(pipe):
    """The same two apps; the brief names Recipe box, and the coder writes into Neighborhood
    board instead (a model that wandered): the diff changes another app's files."""
    run_id, repo = start(pipe, tag="rec51s", seed={**_tree(FIXTURE, "recipe-box/"),
                                                   **_tree(FIXTURE, "neighborhood-board/")},
                         fixture=_new_work_fixture(pipe), app_dir="neighborhood-board",
                         brief_extra=" Build it in the `recipe-box/` folder of this repository.",
                         test_command="npm --prefix recipe-box test")
    return {**run_lineage(pipe, run_id, repo), "root": run_id}


def test_rec51_a_diff_that_changes_another_app_fails_its_gate(rec51_stray):
    first = rec51_stray["runs"][0]
    gate = next(g for g in rec51_stray["gates"] if g["code_task_run_id"] == first["id"])
    assert "stayed_in_app" in (gate["gates_failed"] or []), gate["gates_failed"]
    named = [e["data"] for e in _of(rec51_stray, "code_task.other_app_changed")]
    assert named and named[0]["app"] == "recipe-box"
    assert "neighborhood-board/test/slug-more.test.js" in named[0]["files"]
    # A fact, not a reviewer's opinion: never advisory. Every build of the lineage — the follow-
    # up too, whose own diff no longer touches those files — judges the pull request's whole
    # change, so the gate holds until the files are restored, and the lineage ends at the cap.
    assert all("stayed_in_app" in (g["gates_failed"] or []) for g in rec51_stray["gates"])
    assert rec51_stray["queued_jobs"] == 0 and len(rec51_stray["ran"]) == MAX_BUILDS
    last = rec51_stray["runs"][-1]
    held = json.dumps((last.get("finalize_ctx") or {}).get("checks_failing") or {})
    assert last["status"] != "completed" or "stayed_in_app" in held, (last["status"], held)

