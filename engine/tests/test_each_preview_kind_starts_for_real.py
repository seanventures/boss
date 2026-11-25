"""Every kind of preview a template asks for is STARTED, through BOSS's own start path, and answers.

THE GAP (RCA row 3, BOSSAPP-1433 R5). License Shelf is a Go CLI whose preview is an HTML report.
Its template told the agent to "open the HTML file locally", the preview path assumed an HTTP
server, and every user's preview of it failed. The catalogue's end-to-end test passed throughout:
its start_preview inserted a "running" app_previews row and never started anything.

Here each preview declaration a factory template tells an agent to write (boss.preview.command,
boss.preview.report) is laid down as a tiny app in a verify-green build's retained workspace, and
`preview_service.start_preview` runs for real: it copies the tree, reads the declaration,
launch_app starts it, port discovery waits for it, and the app_previews row is written. The test
then fetches the URL the preview reports and stops it. Nothing of preview_service is patched.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from boss_engine.config import Settings
from boss_engine.services import preview_service

from . import real_preview
from .test_every_template_runs_end_to_end import _tidy  # noqa: F401 - autouse cleanup
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

TEMPLATES = Path(preview_service.__file__).resolve().parents[1] / "data" / "factory_templates"


def _templates() -> list[dict]:
    return [yaml.safe_load(p.read_text()) for p in sorted(TEMPLATES.glob("*.yaml"))]


def test_every_previewed_factory_template_asks_for_a_kind_started_below():
    kinds = {}
    for template in _templates():
        if "preview" not in (template.get("resources") or []):
            continue
        prompt = str(template.get("prompt") or "")
        kinds[template["id"]] = real_preview.declaration_kind(prompt)
        # A desktop opener inside a container is the failure this exists for.
        assert "xdg-open" not in prompt and "open the HTML file locally" not in prompt, \
            template["id"]
    assert kinds, "no factory template asks for a preview"
    assert set(kinds.values()) <= set(real_preview.KINDS), kinds
    assert kinds.get("license-shelf") == "report", kinds


@pytest.mark.parametrize("kind", real_preview.KINDS)
def test_each_preview_declaration_starts_through_start_preview_and_answers_http(
        eng, project, monkeypatch, kind):
    # The uid-pool sandbox is its own subject (test_previews_run_in_the_sandbox,
    # tests/e2e/preview_sandbox); this one is the start itself.
    monkeypatch.setenv("BOSS_SANDBOX_ISOLATION", "off")
    run_id = f"crun_{uuid.uuid4().hex[:12]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, status, phase, title) "
                       "VALUES (:i, :p, 'completed', 'done', 'preview kind')"),
                  {"i": run_id, "p": project})
        real_preview.make_previewable(c, run_id, kind)
    started = None
    try:
        started = preview_service.start_preview(eng, Settings(), run_id, ready_timeout=60)
        assert started["status"] == "running", started
        with eng.begin() as c:
            row = c.execute(text("SELECT status, port, url, pid, command FROM app_previews "
                                 "WHERE id = :i"), {"i": started["id"]}).mappings().one()
        assert row["status"] == "running" and row["port"] == started["port"]
        # The address the gateway proxies to (the URL returned is the gateway's path).
        status, body = real_preview.fetch(row["url"] + "/")
        assert status == 200 and real_preview.MARK[kind] in body, (status, body[:200])
        assert ("boss.preview.report" if kind == "report" else "server.py") in row["command"] \
            or (kind == "report" and "ThreadingHTTPServer" in row["command"]), row["command"]
    finally:
        stopped = real_preview.stop_all(eng, project)
        real_preview.forget_workspace(run_id)
    assert started and started["id"] in stopped
    # Stopped means the process is gone, not only the row.
    pid = started["pid"]
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True
    assert not alive, f"preview process {pid} outlived stop_preview"
