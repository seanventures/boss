"""Fix rounds resume the builder's session instead of cold-respawning (BOSSAPP-1001).

Audit BOSSAPP-997, finding 1: ~78% of claude-sonnet-5 spend was prompt-cache re-read/write from
cold-started CLI sessions — every repair round re-paid the ~34k CLI preamble plus a full repo
re-onboarding to patch work the parent's session already held warm (one measured build: 52.1M of
its 61M cache-read tokens were its 3 implement sessions). The in-run paths (test repair, the warm
fix round, continuations) already resumed; the CROSS-RUN rounds — the review-repair child, the CI
repair, an operator retry — never did.

Now retry_code_task carries the parent attempt's implement session
(state.impl_session -> finalize_ctx.resume_implement_session), and the child's first implement
pass — which for a cross-run repair IS the repair — resumes it, sending only the findings as the
next user turn. The semantics, each pinned below:

  * resume ONLY the implementer's own role, the same engine and the same model — a reviewer or
    test author never inherits the builder's conversation, and a session is never switched to
    another model mid-flight;
  * only a NATIVE session (never a BOSS-issued `boss-sess-` fallback id, which names a
    process-local ledger entry a new worker does not hold);
  * a resume that fails falls back to the cold start it does today — the degradation re-adds
    only the old cost;
  * the per-session budget ceiling (PR #34's _default_ceiling) applies to a resumed session
    exactly as to a cold one;
  * a fresh build (nothing kept to repair) carries no session at all.

Everything here runs on the scripted conformance engine or plain fakes — no model is called.
"""
from __future__ import annotations

import inspect
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from boss_engine.services import code_task_service as cts
from boss_engine.services import engine_contract as ec
from boss_engine.services import governed_code_task_service as gov
from boss_engine.services import scripted_engine
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context

from .test_every_engine_keeps_the_same_contract import ledger  # noqa: F401 (in-memory ledger)

SOURCE = textwrap.dedent(inspect.getsource(gov._run_governed_code_task))  # noqa: SLF001

BASE_PROMPT = ("TASK: implement the cart total (onboarding marker 9c4e). Work ONLY under "
               "`plain/`.\nTEST/BUILD COMMANDS: npm test")
FINDINGS = "- [BLOCKER] plain/src/total.js:7 — the discount is applied twice (marker 55ad)"


# ── The mechanism, proven on the scripted engine (a scripted resume costs no model tokens) ─────

@pytest.fixture
def scripted(tmp_path, ledger):  # noqa: F811 — the imported fixture is used, not shadowed
    scripted_engine.reset()
    raw = scripted_engine.ScriptedEngine(caps={"resume"})
    agent = ec.contracted(raw)
    tree = tmp_path / "tree"
    tree.mkdir()
    set_llm_context(db_engine=ledger, project_id="p_1001", run_id="crun_1001",
                    stage="implement")
    yield agent, tree
    clear_llm_context()
    scripted_engine.reset()


def test_the_repair_round_reuses_the_session_and_resends_no_onboarding(scripted):
    agent, tree = scripted
    first = agent.run_role(BASE_PROMPT, tree)
    sid = first["session_id"]
    assert sid and not sid.startswith(ec.SESSION_PREFIX)   # a native session, not a fallback id

    # The repair pass, exactly as the pipeline sends it: the carried session id, and ONLY the
    # findings as the next user turn.
    second = agent.run_role(gov._cross_run_resume_prompt(FINDINGS, 1), tree,  # noqa: SLF001
                            resume_session_id=sid)
    assert second["session_id"] == sid, "round 2 must REUSE the builder's session"

    calls = scripted_engine.run_state("crun_1001").calls
    assert calls[0]["resumed"] is None
    assert calls[1]["resumed"] == sid, "the engine must be handed the native session id"
    assert "onboarding marker 9c4e" not in calls[1]["full_prompt"], \
        "a resumed round must NOT re-send the task onboarding — that is the whole saving"
    assert "marker 55ad" in calls[1]["full_prompt"], "the findings are the new user turn"
    assert "CONTINUING AN EARLIER SESSION" not in calls[1]["full_prompt"], \
        "a native resume never gets the sessionless seed"


def test_an_engine_without_resume_is_unchanged_by_the_carry(tmp_path, ledger):  # noqa: F811
    """The poorest engine keeps today's behaviour: the carried id is refused up front
    (carried_implement_session), and even a caller that passes one gets the old seeded-new-
    session fallback, never a broken native resume."""
    scripted_engine.reset()
    agent = ec.contracted(scripted_engine.ScriptedEngine(caps=set()))
    assert gov.carried_implement_session(
        {"resume_implement_session": {"session_id": "real-123", "engine": "scripted",
                                      "model": "scripted-v1"}},
        agent=agent, model="scripted-v1") is None
    tree = tmp_path / "t"
    tree.mkdir()
    set_llm_context(db_engine=ledger, project_id="p_1001", run_id="crun_noresume",
                    stage="implement")
    try:
        first = agent.run_role(BASE_PROMPT, tree)
        assert first["session_id"].startswith(ec.SESSION_PREFIX)
        second = agent.run_role("Fix the finding.", tree,
                                resume_session_id=first["session_id"])
        calls = scripted_engine.run_state("crun_noresume").calls
        assert calls[1]["resumed"] is None                      # no native resume was attempted
        assert "CONTINUING AN EARLIER SESSION" in calls[1]["full_prompt"]
        assert second["session_id"].startswith(ec.SESSION_PREFIX)
    finally:
        clear_llm_context()
        scripted_engine.reset()


# ── When a repair may resume, and when it never may (carried_implement_session) ────────────────

GOOD = {"resume_implement_session": {"session_id": "sess-abc", "engine": "scripted",
                                     "model": "m1", "from_run": "crun_parent"}}


def _agent(resume: bool = True, engine: str = "scripted"):
    return SimpleNamespace(engine=engine,
                           capabilities=ec.EngineCapabilities(resume=resume))


def test_a_matching_implement_repair_resumes():
    assert gov.carried_implement_session(GOOD, agent=_agent(), model="m1") == "sess-abc"
    assert gov.carried_implement_session(GOOD, agent=_agent(), model="m1",
                                         stage="solo") == "sess-abc"


def test_a_reviewer_or_test_author_never_inherits_the_builders_conversation():
    for stage in ("review_code", "write_tests", "security_check", "check_tests", "design"):
        assert gov.carried_implement_session(GOOD, agent=_agent(), model="m1",
                                             stage=stage) is None, stage


def test_a_different_model_or_engine_starts_cold():
    assert gov.carried_implement_session(GOOD, agent=_agent(), model="m2") is None
    assert gov.carried_implement_session(GOOD, agent=_agent(engine="codex"), model="m1") is None


def test_an_engine_that_cannot_resume_and_a_fallback_id_start_cold():
    assert gov.carried_implement_session(GOOD, agent=_agent(resume=False), model="m1") is None
    fallback = {"resume_implement_session": {**GOOD["resume_implement_session"],
                                             "session_id": f"{ec.SESSION_PREFIX}deadbeef"}}
    assert gov.carried_implement_session(fallback, agent=_agent(), model="m1") is None


def test_nothing_carried_means_the_cold_start_of_today():
    for fctx in (None, {}, {"resume_implement_session": "sess-abc"},
                 {"resume_implement_session": {"session_id": ""}}):
        assert gov.carried_implement_session(fctx, agent=_agent(), model="m1") is None


# ── The budget ceiling applies to a resumed session exactly as to a cold one ───────────────────

def test_the_per_session_spend_ceiling_rides_the_resumed_session(tmp_path):
    seen: list[dict] = []

    class Engine:
        engine = "fake"
        capabilities = ec.EngineCapabilities(resume=True, budget_limit=True, reports_cost=True)

        def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False,
                     model: Optional[str] = None, resume_session_id: Optional[str] = None,
                     max_turns: Optional[int] = None, parallel_agents: int = 0,
                     max_budget_usd: Optional[float] = None, **_: Any) -> dict:
            seen.append({"resume": resume_session_id, "ceiling": max_budget_usd})
            return {"summary": "ok", "session_id": resume_session_id or "sess-1",
                    "cost_usd_micros": 10, "stop_reason": "done"}

        def run(self, spec, repo, sandbox):  # pragma: no cover — contract surface only
            raise NotImplementedError

    agent = ec.contracted(Engine())
    agent.budget_ceiling = lambda: 3.21                      # what _agent_for_locked binds
    agent.run_role("build it", tmp_path)
    agent.run_role("fix the finding", tmp_path, resume_session_id="sess-1")
    assert [s["ceiling"] for s in seen] == [3.21, 3.21], \
        "PR #34's default ceiling must bound the resumed session too"
    assert seen[1]["resume"] == "sess-1"


# ── What a retry child carries (resumable_implement_session) ──────────────────────────────────

PARENT_STATE = {"impl_session": {"session_id": "sess-p", "engine": "claude_code",
                                 "model": "claude-sonnet-5", "native": True}}


def test_an_unseeded_repair_carries_the_newest_attempts_session():
    rec = cts.resumable_implement_session(
        lambda rid: pytest.fail(f"must not load {rid}: the branch holds run_id's work"),
        run_id="crun_a", state=PARENT_STATE, seed_from="crun_a", seeded=False)
    assert rec == {"session_id": "sess-p", "engine": "claude_code",
                   "model": "claude-sonnet-5", "from_run": "crun_a"}


def test_a_seeded_repair_carries_the_session_of_the_attempt_it_seeds_from():
    older = {"state": {"impl_session": {"session_id": "sess-old", "engine": "claude_code",
                                        "model": "claude-sonnet-5", "native": True}}}
    rec = cts.resumable_implement_session(
        lambda rid: older if rid == "crun_old" else None,
        run_id="crun_a", state=PARENT_STATE, seed_from="crun_old", seeded=True)
    assert rec and rec["session_id"] == "sess-old" and rec["from_run"] == "crun_old", \
        "the session must match the TREE the child starts from, not the newest attempt"


def test_a_fallback_or_absent_session_carries_nothing():
    not_native = {"impl_session": {"session_id": "boss-sess-x", "engine": "aider",
                                   "model": "m", "native": False}}
    assert cts.resumable_implement_session(lambda rid: None, run_id="r", state=not_native,
                                           seed_from="r", seeded=False) is None
    assert cts.resumable_implement_session(lambda rid: None, run_id="r", state={},
                                           seed_from="r", seeded=False) is None
    assert cts.resumable_implement_session(lambda rid: None, run_id="r",
                                           state={"impl_session": "sess"},
                                           seed_from="r", seeded=False) is None


def test_retry_code_task_carries_it_and_a_fresh_build_never_does():
    src = inspect.getsource(cts.retry_code_task)
    assert "None if fresh else resumable_implement_session(" in src, \
        "a fresh build has no work of this lineage to continue"
    assert '"resume_implement_session": resume_impl} if resume_impl else {}' in src
    # Both child-spawning repair paths ride retry_code_task, so both get the carry: the
    # review-repair round and the CI repair.
    assert "nxt = retry_code_task(" in SOURCE
    assert "retried = retry_code_task(" in inspect.getsource(gov.repair_after_ci_failure)


# ── The pipeline's wiring: record, resume, fall back cold ─────────────────────────────────────

def test_the_run_records_the_session_that_wrote_its_tree():
    start = SOURCE.index("def _remember_impl_session(")
    body = SOURCE[start:SOURCE.index("\n        def ", start + 10)]
    assert "'{impl_session}'" in body and '"native"' in body
    assert "if not impl_session_id:" in body
    # Recorded wherever the tree-writing session settles: implement, the in-run fix round, and
    # the secret-repair turn.
    assert SOURCE.count("_remember_impl_session(") >= 4           # the def + three call sites
    impl = SOURCE.index('_install_until_it_passes(a, m, impl_session_id, "implement")')
    assert "_remember_impl_session(a, m)" in SOURCE[impl:impl + 200]
    fix = SOURCE.index('"fix_round")')
    assert "_remember_impl_session(a_fix, m_fix)" in SOURCE[fix:fix + 200]
    sec = SOURCE.index('"secret_repair")')
    assert "_remember_impl_session(a_sec, m_sec)" in SOURCE[sec:sec + 200]


def test_the_cross_run_repair_resumes_only_on_its_first_pass_and_only_in_role():
    at = SOURCE.index("if resume_sid is None and _cross_run_repair and not escalate_impl:")
    block = SOURCE[at:at + 400]
    assert "carried_implement_session(" in block
    assert 'stage="implement" if do_separate_tests else "solo"' in block
    # The in-run rule stays first: attempt > 0 on the same route resumes its OWN session.
    assert SOURCE.index("resume_sid = impl_session_id if (attempt and not escalate_impl") < at
    # The resumed turn carries the findings (and any operator steering), never the base prompt.
    resumed = SOURCE[at:SOURCE.index("impl_session_id = r2.get", at)]
    assert "_cross_run_resume_prompt(_repair_evidence, max(1, attempt))" in resumed
    assert "_guidance_block(_steer) if _steer else" in resumed


def test_a_failed_resume_falls_back_to_exactly_the_cold_start_of_today():
    at = SOURCE.index("code_task.implement_resume_fell_back")
    tail = SOURCE[at:at + 1600]
    assert "r2 = a.run_role(impl_prompt, root, model=m," in tail, \
        "the fallback is the cold full-prompt start — the old cost, nothing more"
    # And the cold prompt for a cross-run repair is unchanged (pinned elsewhere too).
    assert "impl_prompt = _repair_prompt(base_impl_prompt, _ev, max(1, attempt))" in SOURCE


def test_the_resume_is_measured_not_asserted():
    """The before/after story: an event when a session is resumed (with the engine's own word on
    whether the id was reused) and one when the resume fell back. The ledger's session ids and
    `turns` distinguish the rest."""
    at = SOURCE.index("code_task.implement_session_resumed")
    assert '"reused": r2.get("session_id") == resume_sid' in SOURCE[at:at + 600]
    assert "code_task.implement_resume_fell_back" in SOURCE


# ── Self-fix rounds ride the same mechanism (self_fix_build.Build.turn) ───────────────────────

class _FakeBuilder:
    engine = "fake"

    def __init__(self, resume: bool = True, fail_on: Optional[str] = None):
        self.capabilities = ec.EngineCapabilities(resume=resume)
        self.calls: list[Optional[str]] = []
        self.fail_on = fail_on

    def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False,
                 model: Optional[str] = None, resume_session_id: Optional[str] = None,
                 max_turns: Optional[int] = None, **_: Any) -> dict:
        self.calls.append(resume_session_id)
        if resume_session_id and resume_session_id == self.fail_on:
            raise RuntimeError("no conversation found with that session id")
        return {"summary": "ok", "session_id": "sess-live", "cost_usd_micros": 1}


@pytest.fixture
def sf_build(monkeypatch):
    from boss_engine.services import self_fix_build as sfb

    monkeypatch.setattr(sfb.Build, "sfx", lambda self: {
        "project_id": "p_1001", "run_id": None, "budget_usd_micros": 0})
    return sfb.Build(engine=None, settings=None, sfx_id="sfx_1001")


def test_a_self_fix_round_resumes_the_fix_stages_own_session(sf_build, tmp_path):
    agent = _FakeBuilder()
    sf_build.turn(agent, "self_fix_implement", "fix it", tmp_path, read_only=False,
                  resume_key="self_fix_implement")
    sf_build.turn(agent, "self_fix_implement", "round 2: the gate's note", tmp_path,
                  read_only=False, resume_key="self_fix_implement")
    assert agent.calls == [None, "sess-live"], \
        "the second fix round must resume the first round's session"


def test_a_self_fix_reviewer_never_inherits_the_builders_session(sf_build, tmp_path):
    agent = _FakeBuilder()
    sf_build.turn(agent, "self_fix_implement", "fix it", tmp_path, read_only=False,
                  resume_key="self_fix_implement")
    sf_build.turn(agent, "self_fix_review", "judge it", tmp_path, read_only=True)
    assert agent.calls == [None, None], "a review turn carries no resume_key, ever"
    from boss_engine.services import self_fix_build as sfb

    assert "resume_key" not in inspect.getsource(sfb.Build.review)
    assert inspect.getsource(sfb.Build.fix).count('resume_key="self_fix_implement"') == 1


def test_a_failed_self_fix_resume_falls_back_cold_instead_of_stopping(sf_build, tmp_path):
    agent = _FakeBuilder(fail_on="sess-expired")
    sf_build._sessions["self_fix_implement"] = "sess-expired"  # noqa: SLF001
    said = sf_build.turn(agent, "self_fix_implement", "fix it", tmp_path, read_only=False,
                         resume_key="self_fix_implement")
    assert said == "ok" and agent.calls == ["sess-expired", None], \
        "a dead session is a cold start (the old cost), never a Stop('engine')"
    assert sf_build._sessions["self_fix_implement"] == "sess-live"  # noqa: SLF001


def test_a_sessionless_engine_and_a_fallback_id_keep_the_cold_self_fix_rounds(sf_build, tmp_path):
    agent = _FakeBuilder(resume=False)
    sf_build._sessions["self_fix_implement"] = "sess-live"  # noqa: SLF001
    sf_build.turn(agent, "self_fix_implement", "fix it", tmp_path, read_only=False,
                  resume_key="self_fix_implement")
    assert agent.calls == [None], "an engine without caps.resume is never handed a session"

    class BossSid(_FakeBuilder):
        def run_role(self, prompt, cwd, **kw):
            super().run_role(prompt, cwd, **kw)
            return {"summary": "ok", "session_id": f"{ec.SESSION_PREFIX}abcd",
                    "cost_usd_micros": 1}

    fresh = BossSid()
    sf_build._sessions.clear()  # noqa: SLF001
    sf_build.turn(fresh, "self_fix_implement", "fix it", tmp_path, read_only=False,
                  resume_key="self_fix_implement")
    assert "self_fix_implement" not in sf_build._sessions, \
        "a BOSS-issued fallback id is process-local and must never be stored for resume"


def test_the_resume_prompt_tells_the_truth_about_the_tree():
    """The child runs in a FRESH checkout (new directory, seeded or re-cloned): the session's
    memory of file contents may be stale even though paths hold, and a checkpoint rewind may
    have moved the tree. The turn says so instead of letting the model trust its memory."""
    said = gov._cross_run_resume_prompt(FINDINGS, 2)  # noqa: SLF001
    assert "FRESH checkout" in said and "TREE IS THE TRUTH" in said
    assert "do NOT edit, delete, or weaken any test file" in said
    assert FINDINGS in said and "REPAIR ATTEMPT 2" in said
