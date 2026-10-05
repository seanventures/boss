"""One word per state (infra-observability spec §4.2): every raw status of a host, deploy,
preview, app health and pressure band maps through `infra_view.word()` to the one word the
console shows; an unknown raw reads `Unknown` and is logged, never drawn as something else."""
from __future__ import annotations

import logging

import pytest

from boss_engine.services import infra_view as iv

SPEC = {
    ("health", "up"): "Up", ("health", "degraded"): "Slow", ("health", "down"): "Down",
    ("health", "unknown"): "No signal", ("health", "not_deployed"): "Not deployed",
    ("host", "verified"): "Ready", ("host", "degraded"): "Unreachable",
    ("host", "retired"): "Retired",
    ("deploy", "planned"): "Planned", ("deploy", "awaiting_approval"): "Needs you",
    ("deploy", "running"): "Deploying", ("deploy", "applying"): "Deploying",
    ("deploy", "succeeded"): "Live", ("deploy", "healthy"): "Live",
    ("deploy", "failed"): "Failed", ("deploy", "rolled_back"): "Rolled back",
    ("deploy", "rejected"): "Rejected", ("deploy", "cancelled"): "Cancelled",
    ("preview", "starting"): "Starting", ("preview", "running"): "Live",
    ("preview", "failed"): "Failed", ("preview", "stopped"): "Stopped",
    ("preview", "expired"): "Expired",
    ("failure_kind", "clone_failed"): "Clone failed",
    ("failure_kind", "command_missing"): "No start command",
    ("failure_kind", "boot_crash"): "Crashed on boot",
    ("failure_kind", "port_timeout"): "Port never opened",
    ("failure_kind", "no_page_at_root"): "No page at /",
    ("band", "ok"): "OK", ("band", "warn"): "High", ("band", "critical"): "Critical",
}
TONES = {"green", "amber", "red", "accent", "grey", "hollow", "none"}


@pytest.mark.parametrize("kind,raw", sorted(SPEC))
def test_the_spec_words(kind, raw):
    assert iv.word(kind, raw)["word"] == SPEC[(kind, raw)]


def test_every_table_entry_has_a_word_and_a_known_tone():
    for kind, table in iv.WORDS.items():
        for raw, w in table.items():
            assert w["word"] and w["tone"] in TONES, (kind, raw)
            # Copy budget: a state word is short (the spec's longest is "No page at /").
            assert len(w["word"].split()) <= 4, (kind, raw, w)


def test_every_status_the_engine_writes_has_a_word():
    """Statuses the deploy tables and previews actually carry (schema CHECKs and writers)."""
    for raw in ("planned", "awaiting_approval", "running", "healthy", "failed", "rejected"):
        assert iv.word("deploy", raw) != iv.UNKNOWN_WORD, raw          # stack_deployments
    for raw in ("created", "running", "building", "deploying", "succeeded", "deployed",
                "failed", "rolled_back", "blocked"):
        assert iv.word("deploy", raw) != iv.UNKNOWN_WORD, raw          # deployment_runs
    for raw in ("starting", "running", "failed", "stopped", "expired"):
        assert iv.word("preview", raw) != iv.UNKNOWN_WORD, raw


def test_an_unknown_raw_is_unknown_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger=iv.log.name):
        w = iv.word("deploy", "teleported")
    assert w == {"word": "Unknown", "tone": "hollow"}
    assert "teleported" in caplog.text
    assert iv.word("no-such-kind", "up")["word"] == "Unknown"


def test_live_for_the_newest_done_for_the_older():
    assert iv.deploy_word("succeeded", newest_for_target=True)["word"] == "Live"
    assert iv.deploy_word("healthy", newest_for_target=False) == {"word": "Done", "tone": "green"}
    assert iv.deploy_word("failed", newest_for_target=False)["word"] == "Failed"


def test_a_host_says_where_it_is():
    key = "ssh-ed25519 AAAA test"
    assert iv.host_word({"status": "verified", "last_preflight": {"reachable": True}})["word"] \
        == "Ready"
    assert iv.host_word({"status": "verified", "last_preflight": {"reachable": False}})["word"] \
        == "Unreachable"
    assert iv.host_word({"status": "degraded"})["word"] == "Unreachable"
    assert iv.host_word({"status": "retired"})["word"] == "Retired"
    assert iv.host_word({"status": "registered"})["word"] == "Setting up · 1/5"
    authorise = {"status": "registered", "key_secret_ref": "s", "install_command": f"ssh {key}"}
    assert iv.host_word(authorise)["word"] == "Setting up · 2/5"
    verify = {"status": "registered", "key_secret_ref": "s"}
    assert iv.host_word(verify)["word"] == "Setting up · 3/5"
    docker = {"status": "registered", "key_secret_ref": "s",
              "last_preflight": {"reachable": True, "problems": ["docker: not found"]}}
    assert iv.host_word(docker)["word"] == "Setting up · 4/5"
    lost = {"status": "registered", "key_secret_ref": "s",
            "last_preflight": {"problems": ["secret 'ssh_key' is bound but unusable"]}}
    assert iv.host_stage(lost)["stage"] == "credential"


def test_a_failed_preview_says_why():
    w = iv.preview_word({"status": "failed", "failure_kind": "port_timeout"})
    assert w == {"word": "Failed · Port never opened", "tone": "red"}
    assert iv.preview_word({"status": "running"})["word"] == "Live"


def test_a_failure_line_is_at_most_six_words():
    for row in (
        {"status": "failed", "error": {"headline": "Health check failed"}},
        {"status": "failed", "failure_reason": "the deployment worker stopped: TimeoutError: "
                                               "the host took too long to answer"},
        {"status": "failed", "failure_reason": "ssh: Permission denied (publickey) while "
                                               "connecting to the host at 10.0.0.1"},
        {"status": "failed", "steps": [{"name": "install or update: ./install.sh",
                                        "status": "failed"}]},
        {"status": "blocked"},
        {"status": "failed"},
    ):
        line = iv.deploy_failure_line(row)
        assert line and len(line.split()) <= 6, (row, line)
    assert iv.deploy_failure_line({"failure_reason": "Permission denied (publickey) on the "
                                                     "host, the key is not there"}) \
        == "Host refused BOSS's key"


def test_money_is_dollars_never_micros():
    assert iv.usd(0) == "$0.00"
    assert iv.usd(4_000) == "<$0.01"
    assert iv.usd(3_120_000) == "$3.12"
    assert iv.usd(1_234_567_890) == "$1,234.57"


def test_a_short_sha_is_never_invented():
    assert iv.sha7("a1b2c3d4e5f6") == "a1b2c3d"
    assert iv.sha7("main") is None
    assert iv.sha7(None) is None
