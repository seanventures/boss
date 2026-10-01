"""The Codex model picker offers the models the signed-in account has.

The reviewer's model dropdown offered `gpt-5.2-codex` and `o3` — a pair written into the
harness registry — while the operator's own Codex CLI listed gpt-6-astra, gpt-5.6-sol/terra/luna
and gpt-5.5. The picker hid exactly the models the subscription was paying for. Found on boss-1
(2026-09-30). The list now comes from `codex debug models`, the CLI's own catalog.
"""
import json
import stat

from boss_engine.services import cli_harness
from boss_engine.services import project_settings as ps

CATALOG = {"models": [
    {"slug": "gpt-5.5", "visibility": "list", "priority": 13},
    {"slug": "gpt-reserve", "visibility": "hide", "priority": 4},
    {"slug": "gpt-6-astra", "visibility": "list", "priority": 2},
    {"slug": "gpt-5.6-sol", "visibility": "list", "priority": 5},
]}


class _Settings:
    def __init__(self, **kw):
        self.code_agent_engine = kw.get("code_agent_engine", "claude_code")
        self.code_review_engine = kw.get("code_review_engine", "")


def test_the_picker_lists_the_accounts_visible_models_in_the_clis_order(tmp_path, monkeypatch):
    monkeypatch.undo()  # drop the conftest stub; the reader under test is the real one
    monkeypatch.setattr(cli_harness, "_catalog_cache", {})
    binary = tmp_path / "codex"
    binary.write_text("#!/usr/bin/env bash\n"
                      '[ "$1 $2" = "debug models" ] || exit 2\n'
                      f"echo '{json.dumps(CATALOG)}'\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")

    offered = ps.offered_choices(None, _Settings(code_review_engine="codex"), "p",
                                 ps.KNOBS["code_review_model"])
    assert offered == ["", "gpt-6-astra", "gpt-5.6-sol", "gpt-5.5"]


def test_a_cli_that_cannot_answer_offers_nothing_rather_than_a_guess(tmp_path, monkeypatch):
    """No written fallback: a guess is how gpt-5.2-codex/o3 outlived the account's models."""
    monkeypatch.undo()
    monkeypatch.setattr(cli_harness, "_catalog_cache", {})
    _cli(tmp_path, monkeypatch, "codex", "echo 'not signed in' >&2\nexit 1")

    assert cli_harness.offered_models("codex") == ()
    offered = ps.offered_choices(None, _Settings(code_review_engine="codex"), "p",
                                 ps.KNOBS["code_review_model"])
    assert offered == [""], "only the CLI's own default, plus free text in the console"


def test_claude_reports_its_models_through_the_sdk_initialize_request(tmp_path, monkeypatch):
    """Claude Code has no `models` command; its Agent SDK asks with a stream-json
    `initialize` control request, which answers without a model turn. The "default" entry is
    the CLI's default — the picker's empty option — so it is not listed twice."""
    monkeypatch.undo()
    monkeypatch.setattr(cli_harness, "_catalog_cache", {})
    reply = {"type": "control_response", "response": {"subtype": "success",
             "request_id": "boss-models", "response": {"models": [
                 {"value": "default", "displayName": "Default (recommended)"},
                 {"value": "sonnet"}, {"value": "opus"}, {"value": "haiku"}]}}}
    _cli(tmp_path, monkeypatch, "claude",
         'read -r line\n'
         'case "$line" in *initialize*) ;; *) exit 3 ;; esac\n'
         f"echo '{json.dumps({'type': 'system', 'subtype': 'init'})}'\n"
         f"echo '{json.dumps(reply)}'\n"
         "sleep 5")

    assert cli_harness.offered_models("claude_code") == ("sonnet", "opus", "haiku")
    assert ps.offered_choices(None, None, "p", ps.KNOBS["claude_cli_model"]) == [
        "", "sonnet", "opus", "haiku"]


def test_grok_reports_its_models_through_grok_models(tmp_path, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(cli_harness, "_catalog_cache", {})
    _cli(tmp_path, monkeypatch, "grok",
         'echo "Default model: grok-4.6"\necho\necho "Available models:"\n'
         'echo "  * grok-4.6 (default)"\necho "  - grok-4.5"')

    assert cli_harness.offered_models("grok") == ("grok-4.6", "grok-4.5")


def _cli(tmp_path, monkeypatch, name: str, body: str) -> None:
    binary = tmp_path / name
    binary.write_text(f"#!/usr/bin/env bash\n{body}\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")


def test_a_model_from_the_catalog_is_accepted_when_saved():
    assert ps.coerce("code_review_model", "gpt-6-astra") == "gpt-6-astra"
