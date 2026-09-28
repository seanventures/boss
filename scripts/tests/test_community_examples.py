"""The public examples publish startable workflows with their promised briefs."""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ("meeting-to-tickets", "weekly-newsletter", "sales-signal", "trend-to-content")
REQUIRED = {
    "meeting-to-tickets": {"transcript"},
    "weekly-newsletter": {"sectors", "to"},
    "sales-signal": {"company", "salesperson"},
    "trend-to-content": {"trend", "to"},
}


class _Api(http.server.BaseHTTPRequestHandler):
    requests: list[tuple[str, str]] = []
    specs: list[dict] = []
    step_count = 0

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler interface
        self.requests.append(("GET", self.path))
        if self.path.startswith("/v1/settings?"):
            self._send(200, {})
        elif self.path.startswith("/v1/delivery-workflows/custom-steps?"):
            self._send(200, {"steps": []})
        elif self.path.startswith("/v1/delivery-workflows/") and "/spec?" in self.path:
            self._send(404, {"detail": "No workflow yet"})
        else:
            self._send(404, {"detail": "Unexpected test read"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler interface
        self.requests.append(("POST", self.path))
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.startswith("/v1/skills/") and self.path.endswith("/enable"):
            self._send(200, {"config": {"risk_tier": 0}})
        elif self.path == "/v1/role-skills":
            self._send(200, {})
        elif self.path == "/v1/delivery-workflows/custom-steps":
            type(self).step_count += 1
            self._send(201, {"custom_step": {"step_key": f"test_step_{self.step_count}"}})
        elif self.path == "/v1/delivery-workflows/create":
            self._send(201, {"definition": {"id": "wfd_test"}})
        elif self.path == "/v1/delivery-workflows/save":
            self.specs.append(body["spec"])
            self._send(200, {"version": {"version": 1}})
        else:
            self._send(404, {"detail": "Unexpected test write"})

    def log_message(self, *_args: object) -> None:
        pass


def _server():
    _Api.requests, _Api.specs, _Api.step_count = [], [], 0
    server = http.server.HTTPServer(("127.0.0.1", 0), _Api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _run(server: http.server.HTTPServer, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "BOSS_API_KEY": "test-only-key"}
    return subprocess.run(
        [sys.executable, str(script), "--api", f"http://127.0.0.1:{server.server_port}", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=10, check=False,
    )


def test_four_examples_publish_console_inputs_and_stored_default_briefs() -> None:
    server, thread = _server()
    try:
        for name in EXAMPLES:
            result = _run(server, ROOT / "examples" / name / "setup.py")
            assert result.returncode == 0, f"{name}: {result.stdout}\n{result.stderr}"
        assert len(_Api.specs) == len(EXAMPLES)
        for name, spec in zip(EXAMPLES, _Api.specs):
            schema = spec["input_schema"]
            assert set(schema["required"]) == REQUIRED[name]
            assert REQUIRED[name] <= set(schema["properties"])
            assert all(schema["properties"][field].get("title") for field in REQUIRED[name])
            assert all(step["approval"]["mode"] == "manual"
                       for step in spec["steps"] if not step["next"])

        weekly, sales, trend = _Api.specs[1:]
        weekly_steps = {step["id"]: step for step in weekly["steps"]}
        sales_steps = {step["id"]: step for step in sales["steps"]}
        trend_steps = {step["id"]: step for step in trend["steps"]}
        assert weekly_steps["write"]["binding"]["inputs"]["prompt"]["default"]
        assert weekly_steps["send"]["binding"]["inputs"]["subject"]["default"]
        assert sales_steps["assess"]["binding"]["inputs"]["prompt"]["default"]
        assert "DRAFT TO EDIT" in sales_steps["draft"]["binding"]["inputs"]["prompt"]["default"]
        assert sales_steps["notify"]["binding"]["inputs"]["body"] == {
            "from_step": "draft", "field": "text",
        }
        assert trend_steps["assess"]["binding"]["inputs"]["prompt"]["default"]
        assert trend_steps["script"]["binding"]["inputs"]["prompt"]["default"]
    finally:
        server.shutdown()
        thread.join()


def test_all_five_dry_runs_make_only_reads() -> None:
    server, thread = _server()
    try:
        result = _run(server, ROOT / "examples" / "setup_all.py", "--dry-run", "--schedule", "UTC")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.count("would create") == 5
        assert "would schedule it Monday 08:00 UTC" in result.stdout
        assert _Api.requests and all(method == "GET" for method, _ in _Api.requests)
    finally:
        server.shutdown()
        thread.join()
