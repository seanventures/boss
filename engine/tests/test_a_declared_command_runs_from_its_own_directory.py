"""crun_cb6e15f4: helpdesk-ai/boss.preview.command said `cd helpdesk-ai && …`; run from
helpdesk-ai/ it failed and the app read as not serving."""
from boss_engine.services.preview_service import _declared_command_in


def test_a_leading_cd_into_the_declaring_directory_is_dropped(tmp_path):
    app = tmp_path / "helpdesk-ai"
    app.mkdir()
    (app / "boss.preview.command").write_text("cd helpdesk-ai && npm run build && node server.mjs\n")
    assert _declared_command_in(app) == "npm run build && node server.mjs"


def test_a_cd_into_a_real_subdirectory_is_kept(tmp_path):
    app = tmp_path / "web"
    (app / "web").mkdir(parents=True)
    (app / "boss.preview.command").write_text("cd web && npm start")
    assert _declared_command_in(app) == "cd web && npm start"


def test_the_serve_proof_copy_lives_where_the_run_can_enter_it(tmp_path, monkeypatch):
    """The proof copy was a BOSS-owned 0700 mkdtemp under /tmp: launched as the run's uid, the
    declared command could not cd into it (crun_fe2dbbc8, crun_44005c92)."""
    import os
    import stat
    import types

    from boss_engine.services import preview_service, sandbox_identity

    run_dir = tmp_path / "run"
    (run_dir / "work" / "app").mkdir(parents=True)
    (run_dir / "work" / "app" / "x.txt").write_text("x")
    monkeypatch.setattr(sandbox_identity, "current", lambda: types.SimpleNamespace(run_dir=str(run_dir)))
    proof = preview_service._proof_tree(run_dir / "work")
    assert proof.parent == run_dir
    assert stat.S_IMODE(os.stat(proof).st_mode) & 0o005 == 0o005
    assert (proof / "app" / "x.txt").read_text() == "x"
