"""The e2e fakes answer Repo and models' probes the way GitHub and a provider would.

GitHub by token profile (stubs/github_probes.py, from engine/tests/fixtures/github_probe_*.json)
and providers by key value (stubs/model_probes.py) — the same answers the API and engine tests
use, so a journey and a unit test cannot disagree about what a read-only token looks like.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

STUBS = Path(__file__).resolve().parents[1] / "stubs"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_selftest_{name}", STUBS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_profile_answers_the_five_probes():
    probes = _load("github_probes")
    assert {"full", "contents_read_only", "no_pull_requests", "expired", "org_approval_pending",
            "rate_limited", "no_access"} <= set(probes.PROFILES)
    for profile in probes.PROFILES:
        token = f"ghp_e2e_{profile}"
        for method, path in (("GET", "/user"), ("GET", "/user/repos"), ("GET", "/repos/e2e/app"),
                             ("POST", "/repos/e2e/app/git/blobs"),
                             ("POST", "/repos/e2e/app/pulls")):
            assert probes.answer(token, method, path) is not None, (profile, method, path)
    # Not a probe, not a profile token: the git-backed fake answers as before.
    assert probes.answer("ghp_real", "GET", "/user") is None
    assert probes.answer("ghp_e2e_full", "POST", "/repos/e2e/app/git/refs") is None


def test_the_write_and_pr_probes_say_what_the_docs_say():
    probes = _load("github_probes")
    assert probes.answer("ghp_e2e_full", "POST", "/repos/o/r/git/blobs")[0] == 201
    assert probes.answer("ghp_e2e_full", "POST", "/repos/o/r/pulls")[0] == 422
    assert probes.answer("ghp_e2e_contents_read_only", "POST", "/repos/o/r/git/blobs")[0] == 403
    assert probes.answer("ghp_e2e_no_pull_requests", "POST", "/repos/o/r/pulls")[0] == 403
    status, headers, _ = probes.answer("ghp_e2e_expiring", "GET", "/repos/o/r")
    assert status == 200 and headers["github-authentication-token-expiration"].endswith("UTC")


def test_the_model_stub_key_modes():
    probes = _load("model_probes")
    assert probes.answer("sk-stub-ok", "POST", "/v1/chat/completions")[0] == 200
    assert probes.answer("sk-stub-401", "POST", "/v1/chat/completions")[0] == 401
    assert probes.answer("sk-stub-402", "POST", "/v1/chat/completions")[0] == 402
    assert probes.answer("sk-stub-404", "POST", "/v1/chat/completions")[0] == 404
    status, headers, _ = probes.answer("sk-stub-429", "POST", "/v1/chat/completions")
    assert status == 429 and headers["retry-after"] == "600"
    assert probes.answer("anything-else", "POST", "/v1/chat/completions") is None


def test_the_running_fakes_route_through_them():
    fakes = (STUBS / "fakes.py").read_text()
    stub = (STUBS / "model_stub.py").read_text()
    assert "github_probes.answer(token, method, path)" in fakes
    assert "a setup check must never create a branch" in fakes
    assert "model_probes.answer(key, method, path)" in stub
