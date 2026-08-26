"""The console-test CLI: exit codes, and what it refuses to hide.

The browser half is stubbed. What matters here is that a run which lost a feature FAILS, that
a run which merely moved things does not, and that the slow-control report keeps the two
timings apart — those are the properties CI acts on.
"""

import json

import pytest

from boss_engine.cli import console_test as cli
from boss_engine.services.console_test_agent import Baseline, Control, Request


def _baseline(*controls, console="v1"):
    return Baseline(console=console, captured_at="2026-08-26T00:00:00+00:00", controls=list(controls))


def _ctl(area, label, *reqs, **kw):
    return Control(area=area, label=label,
                   requests=[Request(*r) for r in reqs], **kw)


@pytest.fixture
def stub(monkeypatch):
    """Replace the browser with a scripted recording."""
    box = {}

    def fake_learn(config, api_origin=None):
        box["config"] = config
        box["api_origin"] = api_origin
        return box["returns"]

    monkeypatch.setattr(cli, "learn", fake_learn)
    return box


# --------------------------------------------------------------------------- #
# Exit codes — what CI acts on
# --------------------------------------------------------------------------- #
def test_a_lost_feature_fails_the_run(tmp_path, stub, capsys):
    """The whole point. v1 could unbind a target; v2 cannot; the run must be red."""
    base = tmp_path / "v1.json"
    base.write_text(_baseline(
        _ctl("code_tasks", "Unbind target", ("DELETE", "/v1/deployment-targets/7", 200))
    ).to_json())
    stub["returns"] = _baseline(_ctl("boss", "Something else", ("GET", "/v1/orgs", 200)), console="v2")

    rc = cli.main(["verify", "--baseline", str(base), "--url", "http://x/v2"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "MISSING" in out and "Unbind target" in out
    # The report must name the outcome that was lost, not just the button.
    assert "/v1/deployment-targets" in out


def test_relocating_a_feature_does_not_fail_the_run(tmp_path, stub, capsys):
    """v2 renames and moves nearly everything by design. A report that calls that breakage is
    noise, and a noisy report gets ignored — which is how a real MISSING slips through."""
    base = tmp_path / "v1.json"
    base.write_text(_baseline(
        _ctl("code_tasks", "Set target", ("POST", "/v1/code-tasks/9/setup-deployment", 200))
    ).to_json())
    stub["returns"] = _baseline(
        _ctl("missions", "Bind host", ("POST", "/v1/code-tasks/9/setup-deployment", 200)),
        console="v2")

    rc = cli.main(["verify", "--baseline", str(base), "--url", "http://x/v2"])
    assert rc == 0
    assert "MOVED 1" in capsys.readouterr().out


def test_a_control_that_survived_in_name_only_fails(tmp_path, stub, capsys):
    """The dangerous case: the button is there, so it LOOKS migrated, but it calls something
    else. Silently passing this is worse than missing it — it manufactures confidence."""
    base = tmp_path / "v1.json"
    base.write_text(_baseline(
        _ctl("code_tasks", "Deploy", ("POST", "/v1/deployments", 201))
    ).to_json())
    stub["returns"] = _baseline(_ctl("missions", "Deploy", ("GET", "/v1/builds", 200)), console="v2")

    rc = cli.main(["verify", "--baseline", str(base), "--url", "http://x/v2"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "BROKEN" in out
    assert "/v1/deployments" in out and "/v1/builds" in out   # both sides, so it is diagnosable


def test_a_clean_migration_passes(tmp_path, stub):
    base = tmp_path / "v1.json"
    base.write_text(_baseline(_ctl("overview", "Open", ("GET", "/v1/dashboard", 200))).to_json())
    stub["returns"] = _baseline(_ctl("overview", "Open", ("GET", "/v1/dashboard", 200)), console="v2")
    assert cli.main(["verify", "--baseline", str(base), "--url", "http://x/v2"]) == 0


# --------------------------------------------------------------------------- #
# Learn
# --------------------------------------------------------------------------- #
def test_learn_writes_a_baseline_that_can_be_read_back(tmp_path, stub):
    stub["returns"] = _baseline(_ctl("overview", "Open", ("GET", "/v1/dashboard", 200)))
    out = tmp_path / "nested" / "v1.json"      # parent does not exist yet
    assert cli.main(["learn", "--url", "http://x", "--out", str(out)]) == 0
    round_tripped = Baseline.from_json(out.read_text())
    assert round_tripped.controls[0].requests[0].key() == "GET /v1/dashboard"


def test_learn_defaults_to_the_core_areas(tmp_path, stub):
    """Discovery, meetings, coding tasks, ci/cd, monitoring — the ones called core. Recording
    a subset would produce a baseline that passes because it never looked."""
    stub["returns"] = _baseline()
    cli.main(["learn", "--url", "http://x", "--out", str(tmp_path / "b.json")])
    areas = stub["config"].areas
    for core in ("discovery", "meetings", "code_tasks", "delivery", "monitoring"):
        assert core in areas


def test_writes_stay_off_unless_asked(tmp_path, stub):
    stub["returns"] = _baseline()
    cli.main(["learn", "--url", "http://x", "--out", str(tmp_path / "b.json")])
    assert stub["config"].allow_writes is False
    cli.main(["learn", "--url", "http://x", "--out", str(tmp_path / "b.json"), "--allow-writes"])
    assert stub["config"].allow_writes is True


def test_an_unexpected_write_is_reported_not_buried(tmp_path, stub, capsys):
    """If the denylist was wrong and the harness wrote to a live system, that must be the
    loudest thing in the output."""
    stub["returns"] = _baseline(
        _ctl("code_tasks", "Ship it", ("POST", "/v1/deployments", 201),
             note="UNEXPECTED WRITE: issued POST /v1/deployments"))
    cli.main(["learn", "--url", "http://x", "--out", str(tmp_path / "b.json")])
    out = capsys.readouterr().out
    assert "UNEXPECTED WRITES: 1" in out
    assert "Ship it" in out


def test_the_slow_report_separates_waiting_from_working(tmp_path, stub, capsys):
    """"Set target" settled in 115s while its own call took 800ms. Reporting only the total
    would blame the API; reporting only the request would say it was fine. Both, and the gap
    called out."""
    stub["returns"] = _baseline(
        _ctl("code_tasks", "Set target",
             state_after={"elapsed_ms": 115_000, "slowest_request_ms": 800}),
        _ctl("overview", "Refresh",
             state_after={"elapsed_ms": 13_400, "slowest_request_ms": 13_200}),
    )
    cli.main(["learn", "--url", "http://x", "--out", str(tmp_path / "b.json")])
    out = capsys.readouterr().out
    assert "115000ms" in out and "800ms" in out
    assert "waiting on something other than its own request" in out
    # The genuinely slow API call is NOT flagged as a UI defect — different bug, different fix.
    refresh_line = [ln for ln in out.splitlines() if "Refresh" in ln][0]
    assert "waiting on something other" not in refresh_line


def test_fast_controls_are_not_listed(tmp_path, stub, capsys):
    stub["returns"] = _baseline(
        _ctl("overview", "Open", state_after={"elapsed_ms": 120, "slowest_request_ms": 90}))
    cli.main(["learn", "--url", "http://x", "--out", str(tmp_path / "b.json")])
    assert "slowest controls" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Coverage — the agent's own acceptance test
# --------------------------------------------------------------------------- #
def test_a_baseline_that_cannot_catch_known_bugs_is_reported_as_such(tmp_path, capsys):
    """A recording that never visited code_tasks and never timed anything would pass any
    migration. It must not be able to claim coverage."""
    base = tmp_path / "thin.json"
    base.write_text(_baseline(_ctl("overview", "Open")).to_json())
    rc = cli.main(["coverage", "--baseline", str(base)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[NO ]" in out
    assert "could not expose" in out


def test_a_real_baseline_covers_the_known_defects(tmp_path, capsys):
    base = tmp_path / "full.json"
    base.write_text(_baseline(
        _ctl("code_tasks", "Set target", state_after={"elapsed_ms": 115_000}),
        _ctl("overview", "(page load)", ("GET", "/v1/audit/export", 200),
             state_after={"request_count": 1}),
    ).to_json())
    rc = cli.main(["coverage", "--baseline", str(base)])
    assert rc == 0
    assert "[NO ]" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #
def test_the_api_origin_is_passed_through_so_paths_compare(tmp_path, stub):
    """Without it every recorded path keeps its host and v1/v2 never match."""
    stub["returns"] = _baseline()
    cli.main(["learn", "--url", "http://x", "--out", str(tmp_path / "b.json"),
              "--api-origin", "https://api.example"])
    assert stub["api_origin"] == "https://api.example"


def test_the_verify_report_can_be_written_for_ci(tmp_path, stub):
    base = tmp_path / "v1.json"
    base.write_text(_baseline(_ctl("a", "X", ("GET", "/v1/x", 200))).to_json())
    stub["returns"] = _baseline(console="v2")
    report = tmp_path / "report.json"
    cli.main(["verify", "--baseline", str(base), "--url", "http://x/v2", "--out", str(report)])
    data = json.loads(report.read_text())
    assert data["passed"] is False
    assert data["failures"][0]["label"] == "X"


def test_a_command_is_required():
    with pytest.raises(SystemExit):
        cli.main([])
