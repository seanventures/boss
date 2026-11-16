"""Import a global Codex or Claude skill folder (BOSSAPP-840).

A folder is a SKILL.md plus the files beside it, under the directories those tools already
scan for the operator (not a project checkout, and not the network). It is pinned by the
sha256 of that folder the way a URL is pinned by the sha256 of its body, and a harness that
already receives skills (BOSSAPP-854) is handed the folder, siblings included.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)

CODEX_MD = """---
name: house-tone
description: How we write
---
Write short sentences.
"""
CLAUDE_MD = """---
description: How Claude writes
---
Write in the house voice.
"""
SCRIPT = "echo ok\n"
NOTE = "Prefer short sentences.\n"


def _place(root: Path, folder: str, body: str) -> Path:
    skill = root / folder
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(body)
    (skill / "scripts").mkdir()
    (skill / "scripts" / "check.sh").write_text(SCRIPT)
    (skill / "references").mkdir()
    (skill / "references" / "tone.md").write_text(NOTE)
    return skill


def _home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


def _preview(kind: str, locator: str):
    from boss_engine.services import skill_sources

    return skill_sources.preview(
        skill_sources.default_fetcher(None, None, "p"), kind=kind, locator=locator)


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
                       "VALUES (:i, :w, 'folder import', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        for table in ("skill_test_runs", "skill_test_cases", "skill_sources", "change_consents",
                      "skill_versions", "role_skills", "skill_definitions", "eval_runs",
                      "events", "decisions", "project_members", "roles"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


@pytest.mark.parametrize("kind,rel", [
    ("codex", ".codex/skills"),
    ("codex", ".agents/skills"),
    ("claude", ".claude/skills"),
])
def test_a_global_skill_folder_is_read_with_its_siblings(tmp_path, monkeypatch, kind, rel):
    home = _home(tmp_path, monkeypatch)
    body = CODEX_MD if kind == "codex" else CLAUDE_MD
    _place(home / rel, "house-tone" if kind == "codex" else "house-voice", body)
    shown = _preview(kind, "house-tone" if kind == "codex" else "house-voice")
    assert shown["kind"] == kind
    assert shown["files"] == ["references/tone.md", "scripts/check.sh"]
    assert len(shown["sha"]) == 64
    assert _preview(kind, "house-tone" if kind == "codex" else "house-voice")["sha"] == shown["sha"]
    if kind == "claude":
        # Claude's folder name is the skill when the frontmatter leaves name out.
        assert shown["skill"]["skill_key"] == "house_voice"
        assert "house voice" in shown["skill"]["knowledge"]
    else:
        assert shown["skill"]["skill_key"] == "house_tone"
        assert "short sentences" in shown["skill"]["knowledge"]


def test_codex_home_is_the_global_root_when_it_is_set(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _place(home / ".codex" / "skills", "house-tone", CODEX_MD.replace(
        "Write short sentences.", "This is the home copy."))
    codex_home = tmp_path / "codex-home"
    _place(codex_home / "skills", "house-tone", CODEX_MD)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    shown = _preview("codex", "house-tone")
    assert "short sentences" in shown["skill"]["knowledge"]
    assert "home copy" not in shown["skill"]["knowledge"]


def test_a_folder_outside_the_global_directory_is_refused(tmp_path, monkeypatch):
    from boss_engine.services.skill_sources import SkillSourceError

    _home(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere" / "house-tone"
    _place(tmp_path / "elsewhere", "house-tone", CODEX_MD)
    with pytest.raises(SkillSourceError, match="global"):
        _preview("codex", str(outside))
    with pytest.raises(SkillSourceError, match="global"):
        _preview("claude", "../house-tone")


def test_a_symlink_that_leaves_the_skill_folder_is_not_read(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    skill = _place(home / ".claude" / "skills", "house-voice", CLAUDE_MD)
    secret = tmp_path / "secret.txt"
    secret.write_text("OUTSIDE-FOLDER-TEXT\n")
    (skill / "scripts" / "leak.sh").symlink_to(secret)
    shown = _preview("claude", "house-voice")
    assert "scripts/leak.sh" not in shown["files"]
    assert "OUTSIDE-FOLDER-TEXT" not in shown["skill"]["knowledge"]
    # A ~ path that stays inside the global directory is the same folder.
    assert _preview("claude", "~/.claude/skills/house-voice")["sha"] == shown["sha"]

    escaped = tmp_path / "outside-skill"
    _place(tmp_path, "outside-skill", CLAUDE_MD)
    alias = home / ".claude" / "skills" / "alias"
    alias.symlink_to(escaped)
    from boss_engine.services.skill_sources import SkillSourceError

    with pytest.raises(SkillSourceError, match="global"):
        _preview("claude", "alias")


def test_a_pin_that_is_not_the_folder_hash_is_refused(tmp_path, monkeypatch):
    from boss_engine.services.skill_sources import SkillSourceError

    home = _home(tmp_path, monkeypatch)
    _place(home / ".codex" / "skills", "house-tone", CODEX_MD)
    with pytest.raises(SkillSourceError, match="does not match"):
        _preview_pin("codex", "b" * 64)


def _preview_pin(kind: str, pin: str):
    from boss_engine.services import skill_sources

    return skill_sources.preview(
        skill_sources.default_fetcher(None, None, "p"), kind=kind, locator="house-tone", pin=pin)


@pytest.mark.parametrize("kind,rel", [
    ("codex", ".codex/skills"),
    ("claude", ".claude/skills"),
])
def test_import_pins_the_folder_and_every_harness_loads_the_sibling(
        eng, project, tmp_path, monkeypatch, kind, rel):
    from boss_engine.services import harness_delivery as hd
    from boss_engine.services import skill_sources
    from boss_engine.services.code_task_mcp import HarnessMcp, _with_skills

    home = _home(tmp_path, monkeypatch)
    _place(home / rel, "house-tone", CODEX_MD)
    fetcher = skill_sources.default_fetcher(eng, Settings(), project)
    shown = skill_sources.preview(fetcher, kind=kind, locator="house-tone")
    with eng.begin() as c:
        out = skill_sources.import_source(
            c, fetcher, project_id=project, actor="operator", kind=kind,
            locator="house-tone", pin=shown["sha"])
        assert out["source"]["pinned_sha"] == shown["sha"]
        assert out["source"]["kind"] == kind
        c.execute(text(
            "INSERT INTO role_skills (id, project_id, role_key, skill_id, source) "
            "VALUES (:i, :p, 'editor', 'house_tone', 'authored')"),
            {"i": f"rsk_{project}_{kind}", "p": project})
        mcp = _with_skills(HarnessMcp(path="", servers=[]), c, project, "editor")
    bundle = Path(mcp.skills_dir) / "house_tone"
    assert (bundle / "scripts" / "check.sh").read_text() == SCRIPT
    assert (bundle / "references" / "tone.md").read_text() == NOTE
    assert "Write short sentences" in (bundle / "SKILL.md").read_text()

    tree = tmp_path / "work"
    tree.mkdir()
    claude = hd.deliver(engine="claude_code", mode="claude_flag",
                        skills_dir="{workspace}/.claude/skills", mcp=mcp, workspace=tree)
    try:
        copied = tree / ".claude" / "skills" / "boss-house_tone" / "scripts" / "check.sh"
        assert copied.read_text() == SCRIPT
    finally:
        claude.cleanup()

    codex = hd.deliver(engine="codex", mode="codex_home", skills_dir="{home}/skills",
                       mcp=mcp, workspace=tree)
    try:
        env = codex.env_for({"CODEX_HOME": str(tmp_path / "codex-user")})
        loaded = Path(env["CODEX_HOME"]) / "skills" / "house_tone" / "scripts" / "check.sh"
        assert loaded.read_text() == SCRIPT
    finally:
        codex.cleanup()

    process = hd.deliver(engine="process", mode="env_manifest", skills_dir="{env}", mcp=mcp)
    try:
        handed = Path(process.env["BOSS_SKILLS_DIR"]) / "house_tone" / "references" / "tone.md"
        assert handed.read_text() == NOTE
    finally:
        process.cleanup()


def test_sync_parks_a_changed_folder_and_does_not_overwrite(eng, project, tmp_path, monkeypatch):
    from boss_engine.services import skill_sources, skill_versions

    home = _home(tmp_path, monkeypatch)
    skill = _place(home / ".codex" / "skills", "house-tone", CODEX_MD)
    fetcher = skill_sources.default_fetcher(eng, Settings(), project)
    with eng.begin() as c:
        source = skill_sources.import_source(
            c, fetcher, project_id=project, actor="operator", kind="codex",
            locator="house-tone")["source"]
    assert skill_sources.sync_one(eng, Settings(), source["id"])["outcome"] == "unchanged"
    (skill / "scripts" / "check.sh").write_text("echo changed\n")
    parked = skill_sources.sync_one(eng, Settings(), source["id"])
    assert parked["outcome"] == "parked"
    with eng.begin() as c:
        history = skill_versions.list_versions(c, project, "house_tone")
        pinned = c.execute(text("SELECT pinned_sha FROM skill_sources WHERE id = :i"),
                           {"i": source["id"]}).scalar()
    assert len(history) == 1
    assert history[0]["files"]["scripts/check.sh"] == SCRIPT
    assert pinned == source["pinned_sha"]
