"""Skills as a governed supply chain (BOSSAPP-841..844), against a real Postgres.

842 every save path writes a version; restore is a new version; pins hold an agent on one.
843 an agent's change is written only by consuming an approved consent with the SAME diff hash.
841 sources import pinned, and a sync makes a new version (auto, trusted) or parks a consent.
844 saved cases run against a version through eval_harness; a failed graded run gates publish.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)

GUIDE_V1 = "# Tone\nWrite short sentences.\n"
GUIDE_V2 = "# Tone\nWrite short sentences.\nNever use jargon.\n"


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM skill_versions LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'supply chain', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        for table in ("skill_test_runs", "skill_test_cases", "skill_sources", "change_consents",
                      "skill_versions", "role_skills", "skill_definitions", "eval_runs",
                      "events", "decisions", "project_members", "roles"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def _guide(c, project, *, key="house_tone", knowledge=GUIDE_V1, actor="operator", **kw):
    from boss_engine.services.skill_definitions import save_skill

    return save_skill(c, project_id=project, actor=actor, skill_key=key, label="House tone",
                      behaviour_kind="guide", summary="How we write", knowledge=knowledge,
                      risk_tier=0, **kw)


def _versions(c, project, key="house_tone"):
    from boss_engine.services.skill_versions import list_versions

    return list_versions(c, project, key)


def _approve(c, decision_id, actor="ceo"):
    """What DecisionService does on approve for this action, minus the approver plumbing."""
    from boss_engine.services.skill_definitions import activate_authored_skill

    row = c.execute(text("SELECT * FROM decisions WHERE id = :i"),
                    {"i": decision_id}).mappings().one()
    return activate_authored_skill(c, dict(row), actor)


# ── 842: versions ─────────────────────────────────────────────────────────────────────────────


def test_a_console_save_writes_a_version_and_a_no_op_save_does_not(eng, project):
    with eng.begin() as c:
        first = _guide(c, project, mode="create")
        assert first["version_no"] == 1
        second = _guide(c, project, knowledge=GUIDE_V2, mode="update")
        assert second["version_no"] == 2
        again = _guide(c, project, knowledge=GUIDE_V2, mode="update")
        assert again["version_no"] == 2  # same content: not a new version
        history = _versions(c, project)
    assert [v["version_no"] for v in history] == [2, 1]
    assert history[0]["current"] and history[0]["author_kind"] == "person"
    assert history[1]["content"]["knowledge"].strip() == GUIDE_V1.strip()


def test_every_other_save_path_writes_a_version(eng, project):
    """SKILL.md upload, both seeds, an approved agent draft, a restore and a source import."""
    from boss_engine.services import agent_instructions, skill_definitions, skill_versions
    from boss_engine.services.channel_oauth import seed_publishing_standards
    from boss_engine.services.video_planner import HOUSE_STYLE_SKILL_KEY, seed_house_style

    with eng.begin() as c:
        made = agent_instructions.skill_from_file(
            c, project, actor="executive_console", filename="SKILL.md",
            text_="---\nname: release-notes\ndescription: How we write release notes\n---\n"
                  "Lead with what changed for the user.\n")
        key = made["skill"]["skill_key"]
        assert [v["version_no"] for v in _versions(c, project, key)] == [1]

        seed_house_style(c, project_id=project)
        seeded = _versions(c, project, HOUSE_STYLE_SKILL_KEY)
        assert len(seeded) == 1 and seeded[0]["author_kind"] == "system"

        seed_publishing_standards(c, project, "system")
        assert len(_versions(c, project, "youtube_publishing_standards")) == 1

        drafted = skill_definitions.draft_for_approval(
            c, project_id=project, actor="agent:writer", skill_key="lookup_notes",
            label="Look up notes", behaviour_kind="builtin",
            config={"executor_id": "model.generate_text"}, risk_tier=0)
        _approve(c, drafted["decision_id"])
        kept = _versions(c, project, "lookup_notes")
        assert len(kept) == 1 and kept[0]["author_kind"] == "agent"
        assert kept[0]["author_id"] == "agent:writer" and "approved by ceo" in kept[0]["source_note"]

        _guide(c, project, mode="create")
        _guide(c, project, knowledge=GUIDE_V2, mode="update")
        skill_versions.restore(c, project_id=project, actor="operator", skill_key="house_tone",
                               version=1)
        assert len(_versions(c, project)) == 3


def test_restore_is_a_new_version_copying_the_old_one(eng, project):
    from boss_engine.services import skill_versions

    with eng.begin() as c:
        _guide(c, project, mode="create")
        _guide(c, project, knowledge=GUIDE_V2, mode="update")
        diff = skill_versions.diff_versions(c, project, "house_tone", 1, 2)
        assert "+  Never use jargon." in diff["diff"]
        out = skill_versions.restore(c, project_id=project, actor="operator",
                                     skill_key="house_tone", version=1)
        history = _versions(c, project)
    assert out["restored_from"] == 1
    assert [v["version_no"] for v in history] == [3, 2, 1]
    assert history[0]["content"] == history[2]["content"]
    assert history[0]["restored_from"] == history[2]["id"]
    assert "Never use jargon" not in history[0]["content"]["knowledge"]


def test_a_skill_from_before_history_keeps_its_old_content_as_v1(eng, project):
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO skill_definitions (id, project_id, skill_key, label, behaviour_kind, "
            "knowledge, risk_tier, created_by) VALUES ('skd_old_' || :p, :p, 'house_tone', "
            "'House tone', 'guide', :k, 0, 'someone')"), {"p": project, "k": GUIDE_V1})
        _guide(c, project, knowledge=GUIDE_V2, mode="update")
        history = _versions(c, project)
    assert [v["version_no"] for v in history] == [2, 1]
    assert history[1]["author_kind"] == "system" and history[1]["author_id"] == "someone"


def test_a_pinned_agent_runs_its_pinned_version(eng, project):
    from boss_engine.services import skill_versions
    from boss_engine.services.skill_executors import SkillExecutorContext, execute_guide

    with eng.begin() as c:
        _guide(c, project, mode="create")
        _guide(c, project, knowledge=GUIDE_V2, mode="update")
        c.execute(text("INSERT INTO role_skills (id, project_id, role_key, skill_id, source) "
                       "VALUES (:i, :p, 'editor', 'house_tone', 'authored')"),
                  {"i": f"rsk_{project}", "p": project})
        skill_versions.pin(c, project_id=project, actor="operator", role_key="editor",
                           skill_key="house_tone", version=1)
        ctx = SkillExecutorContext(settings=Settings(), connection=c, project_id=project,
                                   actor="editor", caller="editor")
        pinned = execute_guide(ctx, {"_skill_key": "house_tone"})["text"]
        ctx.caller = "someone_else"
        live = execute_guide(ctx, {"_skill_key": "house_tone"})["text"]
    assert "Never use jargon" not in pinned
    assert "Never use jargon" in live


# ── 843: diff-bound consent ───────────────────────────────────────────────────────────────────


def test_draft_skill_without_consent_writes_nothing(eng, project):
    """The BOSSAPP-896 shape: the agent drafts a skill and nobody has approved it."""
    from boss_engine.services.skill_definitions import draft_for_approval, get_skill

    with eng.begin() as c:
        drafted = draft_for_approval(
            c, project_id=project, actor="agent:writer", skill_key="lookup_notes",
            label="Look up notes", behaviour_kind="builtin",
            config={"executor_id": "model.generate_text"}, risk_tier=0)
        assert drafted["status"] == "pending" and drafted["diff_hash"]
        assert get_skill(c, project, "lookup_notes") is None
        assert c.execute(text("SELECT count(*) FROM skill_versions WHERE project_id = :p"),
                         {"p": project}).scalar() == 0
        consent = c.execute(text("SELECT status, diff FROM change_consents WHERE id = :i"),
                            {"i": drafted["consent_id"]}).mappings().one()
    assert consent["status"] == "pending" and "Look up notes" in consent["diff"]


def test_an_agent_cannot_save_a_skill_directly(eng, project):
    from boss_engine.services.change_consent import ConsentRequired
    from boss_engine.services.skill_definitions import get_skill

    with eng.begin() as c:
        with pytest.raises(ConsentRequired):
            _guide(c, project, actor="agent:writer", mode="create")
        assert get_skill(c, project, "house_tone") is None


def test_a_role_agent_acting_under_its_role_name_is_an_agent(eng, project):
    from boss_engine.services.change_consent import ConsentRequired

    with eng.begin() as c:
        c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec) "
                       "VALUES (:i, :p, 'video_editor', 'Video editor', '{}'::jsonb)"),
                  {"i": f"role_{project}", "p": project})
        with pytest.raises(ConsentRequired):
            _guide(c, project, actor="video_editor", mode="create")
        # A person, and the console operator, are exempt.
        assert _guide(c, project, actor="executive_console", mode="create")["version_no"] == 1


def test_an_approved_consent_writes_once_and_only_the_approved_content(eng, project):
    from boss_engine.services import change_consent, skill_versions
    from boss_engine.services.skill_definitions import draft_for_approval, get_skill

    with eng.begin() as c:
        _guide(c, project, mode="create")
        drafted = draft_for_approval(
            c, project_id=project, actor="agent:writer", skill_key="house_tone",
            label="House tone", behaviour_kind="guide", summary="How we write",
            knowledge=GUIDE_V2, risk_tier=0, mode="update")
        # Not yet approved: no write.
        assert "jargon" not in get_skill(c, project, "house_tone")["knowledge"]
        change_consent.approve_for_decision(c, drafted["decision_id"], "ceo")
        # A DIFFERENT proposal finds no consent with its hash.
        with pytest.raises(change_consent.ConsentRequired):
            _guide(c, project, actor="agent:writer", knowledge=GUIDE_V2 + "Extra.\n",
                   mode="update")
        # The approved one is written, once.
        _guide(c, project, actor="agent:writer", knowledge=GUIDE_V2, mode="update")
        assert "jargon" in get_skill(c, project, "house_tone")["knowledge"]
        assert c.execute(text("SELECT status FROM change_consents WHERE id = :i"),
                         {"i": drafted["consent_id"]}).scalar() == "consumed"
        before = skill_versions.content_hash(get_skill(c, project, "house_tone"))
        assert change_consent.consume(c, project_id=project, subject_kind="skill",
                                      subject_key="house_tone",
                                      digest=drafted["diff_hash"]) is None
        assert skill_versions.content_hash(get_skill(c, project, "house_tone")) == before


def test_approving_a_draft_whose_base_moved_refuses(eng, project):
    """A person approves the diff they saw; if the skill changed since, that diff is gone."""
    from boss_engine.services.skill_definitions import (ConsentStale, draft_for_approval,
                                                        get_skill)

    with eng.begin() as c:
        _guide(c, project, mode="create")
        drafted = draft_for_approval(
            c, project_id=project, actor="agent:writer", skill_key="house_tone",
            label="House tone", behaviour_kind="guide", summary="How we write",
            knowledge=GUIDE_V2, risk_tier=0, mode="update")
        _guide(c, project, knowledge="# Tone\nA person rewrote it.\n", mode="update")
        with pytest.raises(ConsentStale):
            _approve(c, drafted["decision_id"])
        assert "A person rewrote it" in get_skill(c, project, "house_tone")["knowledge"]


def test_the_decision_service_approval_writes_and_a_rejection_spends_nothing(eng, project):
    from boss_engine.config import get_settings
    from boss_engine.services.decisions import DecisionService
    from boss_engine.services.skill_definitions import draft_for_approval, get_skill

    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', CAST(:r AS JSONB), "
            "CAST(:perm AS JSONB))"),
            {"i": f"pm_{project}", "p": project, "r": json.dumps(["ceo"]),
             "perm": json.dumps(["decision_resolve"])})
        yes = draft_for_approval(c, project_id=project, actor="agent:writer",
                                 skill_key="lookup_notes", label="Look up notes",
                                 behaviour_kind="builtin",
                                 config={"executor_id": "model.generate_text"}, risk_tier=0)
        no = draft_for_approval(c, project_id=project, actor="agent:writer",
                                skill_key="other_notes", label="Other notes",
                                behaviour_kind="builtin",
                                config={"executor_id": "model.generate_text"}, risk_tier=0)
    service = DecisionService(get_settings())
    with eng.begin() as c:
        service.transition(c, yes["decision_id"], action="approve", actor="ceo", note=None)
        service.transition(c, no["decision_id"], action="reject", actor="ceo", note="no")
    with eng.begin() as c:
        assert get_skill(c, project, "lookup_notes")["enabled"] is True
        assert get_skill(c, project, "other_notes") is None
        assert c.execute(text("SELECT status FROM change_consents WHERE id = :i"),
                         {"i": no["consent_id"]}).scalar() == "rejected"


# ── 841: sources ──────────────────────────────────────────────────────────────────────────────


class FakeUpstream:
    """A repository whose head the test moves; fetch(ref=sha) returns that commit."""

    def __init__(self):
        self.commits = {"a" * 40: _skill_md(GUIDE_V1)}
        self.head = "a" * 40
        self.lie_about = None
        self.asked = []

    def push(self, sha, body):
        self.commits[sha] = _skill_md(body)
        self.head = sha

    def __call__(self, loc, ref):
        from boss_engine.services.skill_sources import Fetched

        self.asked.append(ref)
        sha = ref if ref in self.commits else self.head
        if self.lie_about and ref == self.lie_about:
            sha = "f" * 40
        return Fetched(sha=sha, text=self.commits.get(sha, self.commits[self.head]),
                       files={"scripts/check.sh": "echo ok\n"})


def _skill_md(body):
    return f"---\nname: house-tone\ndescription: How we write\n---\n{body}"


def _import(eng, project, upstream, *, auto=False):
    from boss_engine.services import skill_sources

    with eng.begin() as c:
        return skill_sources.import_source(
            c, upstream, project_id=project, actor="operator", kind="github",
            locator="https://github.com/acme/playbooks/tree/main/skills/house-tone",
            ref="main", auto_update=auto, skill_key="house_tone")


def test_locators_normalise():
    from boss_engine.services.skill_sources import SkillSourceError, parse_locator

    gh = parse_locator("github", "https://github.com/acme/playbooks/tree/main/skills/tone")
    assert (gh.owner, gh.repo, gh.paths) == ("acme", "playbooks", ("skills/tone/SKILL.md",))
    sh = parse_locator("skillsh", "https://skills.sh/acme/playbooks/tone")
    assert sh.paths == ("skills/tone/SKILL.md", "tone/SKILL.md")
    assert parse_locator("url", "https://x.test/SKILL.md").url == "https://x.test/SKILL.md"
    with pytest.raises(SkillSourceError):
        parse_locator("url", "http://x.test/SKILL.md")
    with pytest.raises(SkillSourceError):
        parse_locator("github", "acme")


def test_import_creates_a_pinned_skill_with_its_files(eng, project):
    upstream = FakeUpstream()
    out = _import(eng, project, upstream)
    assert out["source"]["pinned_sha"] == "a" * 40
    with eng.begin() as c:
        history = _versions(c, project)
    assert len(history) == 1 and history[0]["files"] == {"scripts/check.sh": "echo ok\n"}
    assert "imported from github" in history[0]["source_note"]


def test_sync_parks_an_upstream_change_until_a_person_approves_it(eng, project):
    from boss_engine.services import skill_sources

    upstream = FakeUpstream()
    source = _import(eng, project, upstream)["source"]
    assert skill_sources.sync_one(eng, Settings(), source["id"],
                                  fetcher=upstream)["outcome"] == "unchanged"
    upstream.push("b" * 40, GUIDE_V2)
    parked = skill_sources.sync_one(eng, Settings(), source["id"], fetcher=upstream)
    assert parked["outcome"] == "parked"
    with eng.begin() as c:
        assert len(_versions(c, project)) == 1  # never overwritten, not yet versioned
    # The same upstream change is not parked twice.
    assert skill_sources.sync_one(eng, Settings(), source["id"],
                                  fetcher=upstream)["outcome"] == "unchanged"
    with eng.begin() as c:
        _approve(c, parked["decision_id"])
    with eng.begin() as c:
        history = _versions(c, project)
        row = c.execute(text("SELECT pinned_sha, pending_consent_id FROM skill_sources "
                             "WHERE id = :i"), {"i": source["id"]}).mappings().one()
    assert [v["author_kind"] for v in history] == ["sync", "person"]
    assert "jargon" in history[0]["content"]["knowledge"]
    assert row["pinned_sha"] == "b" * 40 and row["pending_consent_id"] is None


def test_sync_applies_by_itself_only_when_auto_update_and_trusted(eng, project):
    from boss_engine.services import skill_sources

    upstream = FakeUpstream()
    source = _import(eng, project, upstream, auto=True)["source"]
    upstream.push("b" * 40, GUIDE_V2)
    # Untrusted: parked even with auto_update on.
    assert skill_sources.sync_one(eng, Settings(), source["id"],
                                  fetcher=upstream)["outcome"] == "parked"
    with eng.begin() as c:
        c.execute(text("UPDATE skill_definitions SET trusted = true WHERE project_id = :p"),
                  {"p": project})
    upstream.push("c" * 40, GUIDE_V2 + "Say what changed first.\n")
    applied = skill_sources.sync_one(eng, Settings(), source["id"], fetcher=upstream)
    assert applied["outcome"] == "applied"
    with eng.begin() as c:
        history = _versions(c, project)
        pinned = c.execute(text("SELECT pinned_sha FROM skill_sources WHERE id = :i"),
                           {"i": source["id"]}).scalar()
    assert history[0]["author_kind"] == "sync" and pinned == "c" * 40
    assert "Say what changed first" in history[0]["content"]["knowledge"]


def test_sync_verifies_the_pin_and_changes_nothing_when_it_does_not_hold(eng, project):
    from boss_engine.services import skill_sources

    upstream = FakeUpstream()
    source = _import(eng, project, upstream)["source"]
    upstream.push("b" * 40, GUIDE_V2)
    upstream.lie_about = "a" * 40  # the pinned commit now resolves to something else
    out = skill_sources.sync_one(eng, Settings(), source["id"], fetcher=upstream)
    assert out["outcome"] == "error" and "pinned commit" in out["error"]
    with eng.begin() as c:
        assert len(_versions(c, project)) == 1
        assert c.execute(text("SELECT count(*) FROM change_consents WHERE project_id = :p"),
                         {"p": project}).scalar() == 0


def test_preview_refuses_a_pin_that_does_not_match(eng, project):
    from boss_engine.services.skill_sources import SkillSourceError, preview

    upstream = FakeUpstream()
    shown = preview(upstream, kind="github", locator="acme/playbooks/skills/house-tone",
                    pin="a" * 40)
    assert shown["sha"] == "a" * 40 and shown["skill"]["label"]
    upstream.lie_about = "a" * 40
    with pytest.raises(SkillSourceError, match="does not match"):
        preview(upstream, kind="github", locator="acme/playbooks", pin="a" * 40)


# ── 844: test cases, version-pinned runs, the publish gate ────────────────────────────────────


def test_cases_run_against_a_version_and_a_failed_grade_gates_publish(eng, project):
    from boss_engine.services import skill_tests, skill_versions

    with eng.begin() as c:
        _guide(c, project, mode="create")
        c.execute(text("INSERT INTO role_skills (id, project_id, role_key, skill_id, source) "
                       "VALUES (:i, :p, 'editor', 'house_tone', 'authored')"),
                  {"i": f"rsk_{project}", "p": project})
        # No cases: publishing is not held back.
        v1 = skill_versions.current(c, project, "house_tone")
        assert skill_tests.publish_gate(c, project, "house_tone", v1["id"])["allowed"]
        skill_tests.save_case(c, project_id=project, actor="operator", skill_key="house_tone",
                              name="greets", case_input={})
        first = skill_tests.run_cases(c, Settings(), project_id=project, actor="operator",
                                      skill_key="house_tone")
        _guide(c, project, knowledge=GUIDE_V2, mode="update")
        second = skill_tests.run_cases(c, Settings(), project_id=project, actor="operator",
                                       skill_key="house_tone")
        # Each run executed THAT version's content.
        assert "jargon" not in first["runs"][0]["output"]["text"]
        assert "jargon" in second["runs"][0]["output"]["text"]
        assert c.execute(text("SELECT suite FROM eval_runs WHERE id = :i"),
                         {"i": second["eval_run_id"]}).scalar() == "skill:house_tone@v2"
        side = skill_tests.compare(c, project, "house_tone")
        assert side["previous_no"] == 1
        assert side["cases"][0]["previous"]["id"] == first["runs"][0]["id"]

        skill_tests.grade_run(c, project_id=project, actor="operator",
                              run_id=second["runs"][0]["id"], grade="fail", notes="too long")
        with pytest.raises(skill_versions.SkillVersionError, match="failed its latest"):
            skill_versions.publish(c, project_id=project, actor="operator",
                                   skill_key="house_tone", version=2)
        # The older version passed: it can be published, which pins the holders to it.
        skill_tests.grade_run(c, project_id=project, actor="operator",
                              run_id=first["runs"][0]["id"], grade="pass")
        out = skill_versions.publish(c, project_id=project, actor="operator",
                                     skill_key="house_tone", version=1)
        assert out["pinned"] and out["holders"] == ["editor"]
        rerun = skill_tests.run_cases(c, Settings(), project_id=project, actor="operator",
                                      skill_key="house_tone", version=2)
        skill_tests.grade_run(c, project_id=project, actor="operator",
                              run_id=rerun["runs"][0]["id"], grade="pass")
        back = skill_versions.publish(c, project_id=project, actor="operator",
                                      skill_key="house_tone", version=2)
        assert back["pinned"] is False  # the current version: holders follow it again
        with pytest.raises(skill_tests.SkillTestError):
            skill_tests.grade_run(c, project_id=project, actor="operator",
                                  run_id=rerun["runs"][0]["id"], grade="maybe")


def test_a_typed_decision_can_grade_a_run(eng, project):
    from boss_engine.services import skill_tests
    from boss_engine.services.typed_decisions import Decision, DecisionBatch

    class Grader:
        def decide(self, context, questions):
            assert set(questions[0].options) == {"pass", "fail"}
            return DecisionBatch(decisions={"grade": Decision("pass", 0.9, "reads well")},
                                 backend="fake", model="m", latency_ms=1)

    with eng.begin() as c:
        _guide(c, project, mode="create")
        skill_tests.save_case(c, project_id=project, actor="operator", skill_key="house_tone",
                              name="greets", case_input={})
        run = skill_tests.run_cases(c, Settings(), project_id=project, actor="operator",
                                    skill_key="house_tone")["runs"][0]
        graded = skill_tests.grade_with_decider(c, project_id=project, run_id=run["id"],
                                                decider=Grader())
    assert graded["grade"] == "pass" and graded["graded_by"] == "typed_decision:fake"


def test_a_skill_that_reaches_outside_is_skipped_not_run(eng, project):
    from boss_engine.services import skill_tests
    from boss_engine.services.skill_definitions import save_skill

    with eng.begin() as c:
        save_skill(c, project_id=project, actor="operator", skill_key="draft_copy",
                   label="Draft copy", behaviour_kind="builtin",
                   config={"executor_id": "model.generate_text"}, risk_tier=1)
        skill_tests.save_case(c, project_id=project, actor="operator", skill_key="draft_copy",
                              name="one", case_input={"prompt": "hi"})
        run = skill_tests.run_cases(c, Settings(), project_id=project, actor="operator",
                                    skill_key="draft_copy")["runs"][0]
    assert run["status"] == "skipped" and "reaches outside" in run["output"]["reason"]
