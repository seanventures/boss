"""A writing agent is told what the build sandbox has, and the image has what builds need.

A HelpDesk build spent steps discovering its environment: curl missing (3 failed probes), argon2
with no arm64 prebuild (node FATAL, repros built in /tmp), a Node without
--experimental-strip-types, and Playwright's browser downloaded at run time (181 s). The image
now carries curl and Node 22, and every writing stage gets the facts read from the process the
agent runs in. A reviewer does not need them.
"""
from __future__ import annotations

from pathlib import Path

from boss_engine.services import code_agent
from tests.test_a_build_fans_out_inside_its_sandbox import _launch

REPO = Path(__file__).resolve().parents[2]


def _facts(monkeypatch, **env) -> str:
    code_agent._SANDBOX_FACTS.clear()
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    try:
        return code_agent.sandbox_facts()
    finally:
        code_agent._SANDBOX_FACTS.clear()


def test_the_facts_name_node_curl_the_browser_and_native_modules(monkeypatch, tmp_path):
    text = _facts(monkeypatch, PLAYWRIGHT_BROWSERS_PATH=str(tmp_path))
    assert "SANDBOX" in text and "bcryptjs" in text and "argon2" in text
    assert "curl" in text and str(tmp_path) in text and "do not download browsers" in text


def test_a_missing_browser_dir_is_not_claimed(monkeypatch, tmp_path):
    assert "Chromium" not in _facts(monkeypatch, PLAYWRIGHT_BROWSERS_PATH=str(tmp_path / "none"))


def test_writing_stages_get_the_facts_and_reviewers_do_not(monkeypatch, tmp_path):
    facts = code_agent.sandbox_facts()
    assert facts in _launch(monkeypatch, tmp_path)["cmd"][-1]
    assert facts not in _launch(monkeypatch, tmp_path, read_only=True)["cmd"][-1]


def test_the_runtime_image_has_curl_and_node_22():
    dockerfile = (REPO / "Dockerfile.community").read_text()
    runtime = dockerfile[dockerfile.index("AS python-runtime"):dockerfile.index("AS api")]
    assert "node:22-bookworm-slim@sha256:" in runtime and "node:20-" not in runtime
    assert " curl " in runtime.replace("\\\n", " ")
