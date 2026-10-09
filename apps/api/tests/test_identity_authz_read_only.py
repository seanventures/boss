"""R5, statically (IDENTITY.md §15.4): reads are declared as reads.

A GET is declared with a read permission (or is public/authenticated); a write declared with a
read permission must say `readonly_post=True` (a search, a preview, a dry run). The full R5 —
calling every GET as a Viewer against a snapshot and diffing rows — needs every handler runnable
in one fixture; until then the GETs the retrofit review found writing are listed below so each one
is converted to a POST, or fixed, on purpose.
"""
from __future__ import annotations

from apps.api.app import identity_guard
from apps.api.app.main import app
from boss_engine.identity.declarations import Permit

READ_ACTIONS = {"read", "read_metadata", "read_all"}

#: GETs declared with a non-read permission, each for a reason.
GET_NOT_READ = {
    "scim:provision": "SCIM 2.0 reads are system-only provisioning",
    "assistant:chat": "the BOSS Agent dock has no separate read key",
    "activity:export": "export downloads are gated on the export permission",
    "feedback:submit": "the caller's own feedback prompts",
    "telemetry:share": "the outbound share bundle (it is what leaves the install)",
    "role_binding:assign": "a project's bindings, for the Admin who manages that project's access "
                           "(role_binding:read is org-only)",
    # App sign-in (APP_TEMPLATES_AND_SECURITY_LOOP.md §6.5): the admin surface has no separate read
    # key; whoever manages pools, clients or pool users is who reads them.
    "oidc:admin": "OIDC pools and consents are read by their administrators",
    "oidc_client:manage": "an app's OIDC client (redirect URIs, roles) is read by who manages it",
    "oidc_pool_user:manage": "a customer pool's users are read by who manages them",
}

#: GETs the review found writing (IDENTITY_PERMISSIONS.md §9 note 6). Shrinking list. Each one
#: is declared `writes_on_get=True` and answers POST at `<path>/load` (identity int-b); see
#: test_identity_authz_writing_gets.py.
GETS_THAT_WRITE = {
    "GET /v1/diag/sandbox-roundtrip", "GET /v1/diag/host-capabilities", "GET /v1/mcp/servers",
    "GET /v1/meeting-roles", "GET /v1/decisions/inbox", "GET /v1/status", "GET /v1/knowledge/eval",
    "GET /v1/hardware/places", "GET /v1/hardware/resolve", "GET /v1/agent-processes/templates",
    "GET /v1/agent-processes/templates/{process_key}", "GET /v1/captain/playbooks",
    "GET /v1/cloud/easy-connect/{session_id}", "GET /v1/telemetry/preview",
    "GET /v1/telemetry/export", "GET /v1/kit", "GET /v1/metatron/session", "GET /v1/previews",
    "GET /v1/preview-sandboxes", "GET /v1/preview-sandboxes/", "GET /v1/preview-sandboxes/{preview_id}",
    "GET /v1/ai-sre/runbooks", "GET /v1/workflow-runs/{run_id}/chain", "GET /v1/elastic/status",
    "GET /v1/elastic/instances", "GET /v1/provider-usage", "GET /v1/provider-usage/summary",
}


def _entries():
    for entry in identity_guard.route_inventory(app):
        for label in entry.labels():
            yield label, entry.access


def test_every_get_is_declared_as_a_read():
    wrong = []
    for label, access in _entries():
        if label.startswith("GET ") and isinstance(access, Permit):
            action = access.permission.split(":", 1)[1]
            if (action not in READ_ACTIONS and access.permission not in GET_NOT_READ
                    and label not in GETS_THAT_WRITE):
                wrong.append(f"{label}: {access.permission}")
    assert not wrong, wrong


def test_a_write_declared_with_a_read_permission_says_it_writes_nothing():
    """...or is the POST twin of a writing GET (identity_guard.writing_get): the same endpoint
    and declaration as the GET, so the same read permission."""
    wrong = [f"{label}: {access.permission}" for label, access in _entries()
             if not label.startswith(("GET ", "WS ", "MOUNT ")) and isinstance(access, Permit)
             and access.permission.split(":", 1)[1] in READ_ACTIONS and not access.readonly_post
             and not access.writes_on_get]
    assert not wrong, wrong


def test_the_writing_gets_list_names_real_routes():
    labels = {label for label, _ in _entries()}
    assert GETS_THAT_WRITE <= labels, sorted(GETS_THAT_WRITE - labels)
