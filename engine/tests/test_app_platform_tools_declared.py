"""The template and security-loop tools (tools/app_platform_tools.py) declare what their routes
declare (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.6, §4.3), confine the ids they take, and import /
approve stay off the tool surface. No database: the behaviour is proven on a real one by
apps/api/tests/test_app_platform_mcp_tools.py."""
from __future__ import annotations

from boss_engine.tools import build_default_registry
from boss_engine.tools.builtin import build_discuss_read_registry
from boss_engine.tools.object_args import CONFINED, UNCONFINED

DECLARED = {
    # tool: (kind, permission, also_requires)
    "list_templates": ("read", "template:read", ()),
    "describe_template": ("read", "template:read", ()),
    "template_status": ("read", "project:read", ()),
    "apply_template": ("write", "workflow_run:start", ("template:read",)),
    "run_security_scan": ("write", "security_scan:run", ()),
    "security_scan_status": ("read", "project:read", ()),
    "list_security_findings": ("read", "project:read", ()),
    "security_report": ("read", "project:read", ()),
    "security_review_status": ("read", "project:read", ()),
}


def test_each_tool_declares_its_routes_permissions():
    reg = build_default_registry()
    for name, (kind, permission, also) in DECLARED.items():
        spec = reg.get(name)
        assert (spec.kind, spec.permission, tuple(spec.also_requires)) == (kind, permission, also), name
    assert reg.get("apply_template").reversibility == "costs_money"   # it starts a paid build


def test_the_ids_they_take_are_confined_to_the_calls_project():
    assert CONFINED["apply_template"] == {"extends_run_id": "code_task"}
    assert CONFINED["security_scan_status"] == {"scan_id": "security_scan"}
    assert CONFINED["list_security_findings"] == {"scan_id": "security_scan"}
    assert CONFINED["security_review_status"] == {"run_id": "workflow_run"}
    # a template is the org's, read only in the call's project's org
    assert "template_id" in UNCONFINED["apply_template"] and "template_id" in UNCONFINED["describe_template"]


def test_import_and_approve_stay_console_and_api_only():
    names = set(build_default_registry().names())
    for forbidden in ("import_template", "approve_template", "reject_template", "trial_template",
                      "delete_template", "triage_security_finding"):
        assert forbidden not in names


def test_chat_reaches_the_reads_and_apply():
    discuss = set(build_discuss_read_registry().names())
    assert set(DECLARED) <= discuss
