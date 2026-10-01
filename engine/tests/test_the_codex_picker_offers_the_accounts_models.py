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


def test_a_cli_that_cannot_answer_falls_back_to_the_known_names(tmp_path, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(cli_harness, "_catalog_cache", {})
    binary = tmp_path / "codex"
    binary.write_text("#!/usr/bin/env bash\necho 'not signed in' >&2\nexit 1\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")

    assert cli_harness.offered_models("codex") == ("gpt-5.2-codex", "o3")


def test_a_model_from_the_catalog_is_accepted_when_saved():
    assert ps.coerce("code_review_model", "gpt-6-astra") == "gpt-6-astra"
