from scripts.qa.working_software_gate import (
    is_app_run,
    is_fixture_html,
    is_working_app_html,
    parse_args,
    pick_working_run,
    run_has_passing_verify,
)


def test_parse_args_dry_run():
    args = parse_args(["--dry-run", "--project-id", "boss-dogfood"])
    assert args.dry_run is True
    assert args.project_id == "boss-dogfood"


def test_run_has_passing_verify_from_state():
    assert run_has_passing_verify({"state": {"tests_passed": True}}) is True
    assert run_has_passing_verify({"state": {"test_exit_code": 0, "test_command": "npm run verify"}}) is True
    assert run_has_passing_verify({"state": {"tests_passed": False, "test_exit_code": 1}}) is False


def test_pick_working_run_prefers_completed_verify():
    runs = [
        {"id": "sim", "status": "simulated", "state": {}},
        {
            "id": "bug",
            "status": "completed",
            "title": "Web app — BugBox",
            "state": {"tests_passed": True, "impl_files": ["src/App.tsx"]},
            "finalize_ctx": {"web_app_build": True},
        },
    ]
    picked = pick_working_run(runs)
    assert picked is not None
    assert picked["id"] == "bug"
    assert is_app_run(picked) is True


def test_fixture_html_is_not_working_software():
    html = "<html><body>fixture — not a sandboxed app</body></html>"
    assert is_fixture_html(html) is True
    assert is_working_app_html(html) is False


def test_bugbox_preview_html_counts_as_working_software():
    html = (
        "<!doctype html><html><head><title>BugBox — capture, track, resolve</title>"
        "</head><body><div id=\"root\"></div></body></html>"
    )
    assert is_fixture_html(html) is False
    assert is_working_app_html(html) is True
