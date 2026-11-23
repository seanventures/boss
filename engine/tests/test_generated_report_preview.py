import json
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pytest

from boss_engine.services import preview_service
from boss_engine.services.report_preview import report_path, serve


def test_report_declaration_runs_generation_in_app_directory(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "boss.preview.report").write_text(json.dumps({
        "path": "report.html", "prepare": "go run . fixtures",
    }))
    run = preview_service.how_app_runs(tmp_path, ["app/boss.preview.report"])
    assert run.source == "declared"
    assert "cd app &&" in run.shell()
    assert "go run . fixtures" in run.shell()
    assert "ThreadingHTTPServer" in run.shell()


@pytest.mark.parametrize("value", ["../report.html", "/tmp/report.html", ".env", "app.py"])
def test_report_cannot_escape_or_serve_arbitrary_files(tmp_path, value):
    with pytest.raises(ValueError):
        report_path(tmp_path, value)


def test_report_symlink_cannot_escape(tmp_path):
    (tmp_path / "report.html").symlink_to(tmp_path.parent / "outside.html")
    with pytest.raises(ValueError):
        report_path(tmp_path, "report.html")


def test_missing_artifact_is_actionable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="configure boss.preview.report prepare"):
        serve("report.html")


def test_other_lineage_cannot_select_report(tmp_path):
    (tmp_path / "boss.preview.report").write_text('{"path":"report.html"}')
    assert preview_service.find_run_declaration(tmp_path, ["other/main.go"]) is None


def test_report_change_requires_serve_proof():
    assert preview_service.touches_runnable_source(["app/boss.preview.report"])


def test_generated_report_serves_without_exposing_repo(tmp_path):
    (tmp_path / ".env").write_text("secret")
    (tmp_path / "boss.preview.report").write_text(json.dumps({
        "path": "report.html", "prepare": "printf '<h1>License report</h1>' > report.html",
    }))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    plan = preview_service.how_app_runs(tmp_path, ["boss.preview.report"])
    process = subprocess.Popen(plan.command, cwd=tmp_path, env={**os.environ, "PORT": str(port)},
                               start_new_session=True)
    try:
        for attempt in range(100):
            try:
                with urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                    assert response.read() == b"<h1>License report</h1>"
                break
            except URLError:
                if process.poll() is not None:
                    pytest.fail("Report preview process exited")
                time.sleep(0.05)
        else:
            pytest.fail("Report preview did not start")
        for route in ("/.env", "/../.env", "/%2e%2e/.env", "/boss.preview.report"):
            with pytest.raises(HTTPError) as error:
                urlopen(f"http://127.0.0.1:{port}{route}", timeout=1)
            assert error.value.code == 404
    finally:
        import signal

        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)


@pytest.mark.parametrize("body, said", [("{not json", "not valid JSON"),
                                        ('{"prepare": "make"}', "has no HTML path"),
                                        ('{"path": "r.html", "prepare": ["make"]}', "one shell command")])
def test_a_bad_report_declaration_says_how_to_write_it(tmp_path, body, said):
    (tmp_path / "boss.preview.report").write_text(body)
    with pytest.raises(ValueError, match=said) as refused:
        preview_service.find_run_declaration(tmp_path, ["boss.preview.report"])
    assert '{"path": "report.html", "prepare":' in str(refused.value)


def test_a_bad_report_declaration_fails_the_serve_proof_with_its_sentence(tmp_path):
    """Review of PR #300: the proof records the fix, it does not raise out of the build step."""
    (tmp_path / "boss.preview.report").write_text("{not json")
    record = preview_service.prove_app_serves(tmp_path, ["boss.preview.report"], timeout=5)
    assert record["ok"] is False
    assert "boss.preview.report is not valid JSON" in record["error"]


def test_a_failing_prepare_says_what_to_fix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match=r"prepare failed \(exit 3\); fix its command"):
        serve("report.html", "exit 3")
