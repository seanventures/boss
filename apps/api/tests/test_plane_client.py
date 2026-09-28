"""P2/P4 — outbound Plane client: state resolution + set_state/add_comment, all best-effort."""
from types import SimpleNamespace

from apps.api.app.services.plane_client import PlaneClient


def _client(**over):
    s = SimpleNamespace(plane_base_url="https://plane.local.example.test", plane_workspace="goldeneye",
                        plane_project_url="", plane_api_token="tok")
    for k, v in over.items():
        setattr(s, k, v)
    return PlaneClient(s)


def test_enabled_requires_base_and_token():
    assert _client().enabled() is True
    assert _client(plane_base_url="", plane_project_url="").enabled() is False
    assert _client(plane_api_token="").enabled() is False


def test_ctx_derives_from_project_url_when_base_unset():
    c = _client(plane_base_url="", plane_workspace="",
                plane_project_url="https://plane.local.example.test/goldeneye/projects/abc/issues")
    base, ws, token = c._ctx()
    assert base == "https://plane.local.example.test" and ws == "goldeneye"


def test_set_state_patches_resolved_id(monkeypatch):
    c = _client()
    calls = []
    monkeypatch.setattr(c, "_states", lambda pid, force=False: {"in progress": "sid1", "done": "sid2"})
    monkeypatch.setattr(c, "_request", lambda method, pid, path, body=None: calls.append((method, path, body)) or {"__ok__": 200})
    assert c.set_state("proj", "iss", "In Progress") is True
    assert calls == [("PATCH", "issues/iss/", {"state": "sid1"})]
    # unknown state -> False, no PATCH
    monkeypatch.setattr(c, "_states", lambda pid, force=False: {})
    assert c.set_state("proj", "iss", "Nonexistent") is False


def test_add_comment_posts(monkeypatch):
    c = _client()
    calls = []
    monkeypatch.setattr(c, "_request", lambda method, pid, path, body=None: calls.append((method, path, body)) or {"__ok__": 200})
    assert c.add_comment("proj", "iss", "<p>hi</p>") is True
    assert calls[0][0] == "POST" and calls[0][1] == "issues/iss/comments/" and calls[0][2] == {"comment_html": "<p>hi</p>"}


def test_create_issue_returns_canonical_provider_payload(monkeypatch):
    c = _client()
    calls = []
    monkeypatch.setattr(
        c,
        "_request",
        lambda method, pid, path, body=None: calls.append(
            (method, pid, path, body)
        )
        or {"id": "issue-7", "sequence_id": 31, "name": body["name"]},
    )

    issue = c.create_issue(
        "proj",
        name="Rank customer-facing work",
        description_html="<p>Measured priority rationale.</p>",
    )

    assert issue["id"] == "issue-7"
    assert calls == [
        (
            "POST",
            "proj",
            "issues/",
            {
                "name": "Rank customer-facing work",
                "description_html": "<p>Measured priority rationale.</p>",
            },
        )
    ]


def test_secret_ref_pointer_is_preferred_over_raw_token(monkeypatch):
    c = _client(plane_api_token="sref_plane_token", plane_api_key="sref_plane_token")
    monkeypatch.setattr(c, "_resolve_secret_ref", lambda ref_id: "tok-from-ref")
    assert c._secret_ref_id() == "sref_plane_token"
    assert c._token() == "tok-from-ref"


def test_bound_secret_ref_does_not_fall_back_to_env(monkeypatch):
    c = _client(plane_api_secret_ref_id="sref_plane", plane_api_token="env-token")
    monkeypatch.setattr(
        c,
        "_resolve_secret_ref",
        lambda ref_id: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    # fail-closed: bound ref that cannot resolve does not become the env token
    assert c._token() == ""


def test_request_failure_is_fail_safe(monkeypatch):
    # a network error yields None (logged), never raises -> set_state/add_comment return False
    c = _client()
    monkeypatch.setattr(c, "_states", lambda pid, force=False: {"done": "sid"})
    monkeypatch.setattr(c, "_request", lambda *a, **k: None)
    assert c.set_state("proj", "iss", "Done") is False
    assert c.add_comment("proj", "iss", "x") is False
