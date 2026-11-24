"""Start a preview the way BOSS does, for tests that must not fake the last mile (R5).

THE RULE (BOSSAPP-1433 R5). A P0/P1 test may mock the model, never the step that fails for
users. The catalogue's end-to-end test inserted a "running" app_previews row and started
nothing, so a CLI whose preview was an HTML report (License Shelf, RCA row 3) passed it while
every user's preview of it failed. These helpers give a test a tiny, real app of each preview
declaration kind the factory templates tell agents to write, make a build run previewable the
way a verify-green local build is (its retained workspace under boss-sandboxes/<run id>), and
call `preview_service.start_preview` itself: copy, declaration, launch_app, port discovery, the
app_previews row. Every process started is stopped with `preview_service.stop_preview`.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import textwrap
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import text

#: The preview declarations a build may write (preview_service.RUN_DECLARATION_INSTRUCTION):
#: `command` is boss.preview.command (a server on $PORT), `report` is boss.preview.report (a
#: generated HTML file BOSS serves itself, License Shelf's kind).
KINDS = ("command", "report")

MARK = {"command": "<h1>Command app answers</h1>", "report": "<h1>License report</h1>"}

_SERVER = textwrap.dedent("""\
    import os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    BODY = %r.encode()


    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

        def log_message(self, *_):
            pass


    if __name__ == "__main__":
        ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), Handler).serve_forever()
    """)


def declaration_kind(template_text: str) -> str:
    """The declaration a factory template's prompt tells the agent to write."""
    return "report" if "boss.preview.report" in template_text else "command"


def write_app(root: Path, kind: str) -> Path:
    """A minimal app of `kind`, as an agent following the template would leave it."""
    root.mkdir(parents=True, exist_ok=True)
    if kind == "command":
        (root / "requirements.txt").write_text("")
        (root / "server.py").write_text(_SERVER % MARK["command"])
        (root / "boss.preview.command").write_text("python3 server.py\n")
    elif kind == "report":
        # License Shelf's shape: a Go CLI whose preview is a report generated from fixtures.
        (root / "go.mod").write_text("module example.com/licenseshelf\n\ngo 1.22\n")
        (root / "fixtures").mkdir(exist_ok=True)
        (root / "fixtures" / "report.html").write_text(MARK["report"])
        (root / "boss.preview.report").write_text(json.dumps(
            {"path": "out/report.html",
             "prepare": "mkdir -p out && cp fixtures/report.html out/report.html"}))
    else:
        raise ValueError(kind)
    return root


def workspace_of(run_id: str) -> Path:
    """Where a local build's retained workspace lives (find_local_run_workspace's first root)."""
    return Path(tempfile.gettempdir()) / "boss-sandboxes" / run_id


def make_previewable(c, run_id: str, kind: str) -> Path:
    """A finished build with no pull request whose tests passed, its app left in its retained
    workspace: exactly what assert_previewable and find_local_run_workspace accept."""
    c.execute(text("UPDATE code_task_runs SET state = COALESCE(state, '{}'::jsonb) "
                   "|| '{\"tests_passed\": true}'::jsonb, pull_request_url = NULL WHERE id = :i"),
              {"i": run_id})
    return write_app(workspace_of(run_id), kind)


def forget_workspace(run_id: str) -> None:
    shutil.rmtree(workspace_of(run_id), ignore_errors=True)


def fetch(url: str, timeout: float = 5.0) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 — loopback
        return response.status, response.read().decode()


class SameTransaction:
    """An engine whose every `begin()` is a savepoint on one open connection.

    The catalogue test drives the runner inside one transaction it never commits (its fakes'
    rows are visible only there), so the real start_preview — which opens its own transactions
    — would not see the build it previews. This hands start_preview that transaction instead;
    the code it runs is unchanged."""

    def __init__(self, connection):
        self._c = connection

    @contextmanager
    def begin(self):
        with self._c.begin_nested():
            yield self._c

    def connect(self):
        return _Borrowed(self._c)


class _Borrowed:
    def __init__(self, connection):
        self._c = connection

    def __enter__(self):
        return self._c

    def __exit__(self, *_):
        return False


def stop_all(engine, project_id: str) -> list[str]:
    """Stop every preview of the project still holding a process; returns their ids."""
    from boss_engine.services import preview_service

    with engine.begin() as c:
        ids = [r[0] for r in c.execute(text(
            "SELECT id FROM app_previews WHERE project_id = :p AND pid IS NOT NULL "
            "AND status NOT IN ('stopped', 'failed')"), {"p": project_id}).all()]
    for preview_id in ids:
        preview_service.stop_preview(engine, preview_id)
    return ids
