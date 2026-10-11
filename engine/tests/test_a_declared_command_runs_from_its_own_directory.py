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
