"""F1 · the egress proxy, hardened (security findings #3, #16 and the egress MEDIUM/LOW items).

Each test fails without the fix it names:
  #3   a path is decoded and canonicalised BEFORE any rule sees it; `..`, `.`, `//` and encoded
       separators are refused outright, and the upstream receives exactly the judged path;
  M    an agent cannot APPROVE a pull request, nor PATCH one that is not its own run's (or its base);
  M    method-override headers refuse the request; hop-by-hop and unknown headers never cross
       (an allowlist, not a blocklist); a duplicated query key is refused;
  M    every pushed commit's workflows must equal its first parent's: an intermediate commit that
       edits a workflow and a later one that reverts it, and a commit that reuses an existing tree
       with other workflows, are refused; a thin pack is resolved against the remote and checked;
  L    a push larger than the pack limit is refused before it is parsed;
  #16  the egress credential works only from its sandbox's network identity (and binds on first use
       when none was known).
"""
from __future__ import annotations

import hashlib
import json
import zlib

import pytest
from sqlalchemy import text

from boss_engine.identity import egress_rules, runs

from ._identity_agents_support import build_env
from .test_identity_agents_egress_and_cascade import (  # noqa: F401 — shared push builders
    OLD,
    RUN_BRANCH,
    _api,
    _basic,
    _change,
    _commit,
    _git,
    _obj,
    _pack,
    _push,
    _setup,
    _tree,
)

from apps.egress.boss_egress.proxy import EgressRequest, upstream_url  # noqa: E402


@pytest.fixture()
def env(monkeypatch):
    return build_env(monkeypatch)


# ---- #3 path traversal ------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "/repos/acme/web/contents/../pulls/1/merge",
    "/repos/acme/web/contents/%2e%2e/pulls/1/merge",
    "/repos/acme/web/contents/%2E%2E/pulls/1/merge",
    "/repos/acme/web/contents/x%2f..%2fpulls/1/merge",
    "/repos/acme/web/contents/./README.md",
    "/repos/acme/web/contents//README.md",
    "/repos/acme/web/commits/../../infra/hooks",
    "/repos/acme/web/contents/a%5c..%5cb",
    "/repos/acme/web/contents/%252e%252e/x",
])
def test_3_a_dot_segment_or_encoded_separator_is_refused_before_any_rule(env, monkeypatch, raw):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    body = json.dumps({"branch": "boss/run_ct-task1-x", "content": "eA==", "message": "m"}).encode()
    for method in ("GET", "PUT"):
        resp = proxy.handle(EgressRequest(method, "/github-api" + raw, "",
                                          {"authorization": f"Bearer {shim.egress_credential}"}, body))
        assert resp.status == 403, (method, raw)
    assert up.calls == []


def test_3_the_rules_match_exactly_the_path_the_upstream_receives(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    resp = proxy.handle(EgressRequest("GET", "/github-api/repos/acme/web/contents/docs/a%20b.md", "ref=main",
                                      {"authorization": f"Bearer {shim.egress_credential}"}))
    assert resp.status == 200
    assert up.calls[-1][1] == "https://api.github.com/repos/acme/web/contents/docs/a%20b.md?ref=main"
    # what the client library sends is exactly the canonical path the rules judged
    assert upstream_url("https://api.github.com", "/repos/acme/web/contents/x", "") == \
        "https://api.github.com/repos/acme/web/contents/x"
    with pytest.raises(egress_rules.PathRefused):
        egress_rules.canonical_path("/repos/acme/web/contents/../pulls/1/merge")


# ---- pull requests ---------------------------------------------------------------------------------------

def test_an_agent_can_never_approve_a_pull_request(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    for event in ("APPROVE", "approve"):
        assert _api(proxy, shim, "POST", "/repos/acme/web/pulls/7/reviews", {"event": event, "body": "lgtm"}).status == 403
    assert up.calls == []
    assert _api(proxy, shim, "POST", "/repos/acme/web/pulls/7/reviews", {"event": "COMMENT", "body": "nit"}).status == 200


def test_an_agent_patches_only_its_own_runs_pull_request_and_never_its_base(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    hub.pull_heads = {7: "main-feature-of-a-human", 8: "boss/run_ct-task1-feature"}
    assert _api(proxy, shim, "PATCH", "/repos/acme/web/pulls/7", {"title": "hijack"}).status == 403
    assert _api(proxy, shim, "PATCH", "/repos/acme/web/pulls/8", {"base": "release"}).status == 403
    assert _api(proxy, shim, "PATCH", "/repos/acme/web/pulls/99", {"title": "x"}).status == 403   # unknown PR
    assert not [c for c in up.calls if c[0] == "PATCH"]
    assert _api(proxy, shim, "PATCH", "/repos/acme/web/pulls/8", {"title": "Better title"}).status == 200


# ---- headers ---------------------------------------------------------------------------------------------

def test_method_override_is_refused_and_only_allowlisted_headers_cross(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    resp = proxy.handle(EgressRequest("GET", "/github-api/repos/acme/web/pulls/3", "",
                                      {"authorization": f"Bearer {shim.egress_credential}",
                                       "x-http-method-override": "DELETE"}))
    assert resp.status == 403 and up.calls == []
    proxy.handle(EgressRequest("GET", "/github-api/repos/acme/web/pulls/3", "",
                               {"authorization": f"Bearer {shim.egress_credential}", "accept": "application/json",
                                "connection": "keep-alive, x-secret", "keep-alive": "timeout=5", "te": "trailers",
                                "transfer-encoding": "chunked", "upgrade": "h2c", "x-forwarded-for": "10.0.0.1",
                                "x-unknown-new-credential": "abc", "proxy-authorization": "Basic x"}))
    sent = {k.lower() for k in up.calls[-1][2]}
    assert sent == {"accept", "authorization"}


def test_a_duplicated_query_key_is_refused(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    resp = proxy.handle(EgressRequest("GET", "/github/acme/web.git/info/refs",
                                      "service=git-upload-pack&service=git-receive-pack",
                                      {"authorization": _basic(shim.egress_credential)}))
    assert resp.status == 403 and up.calls == []


# ---- pushes: intermediate commits, reused trees, thin packs, size ------------------------------------

def test_an_intermediate_commit_that_edits_a_workflow_is_refused_even_if_reverted(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    c1, objs1 = _change({".github/workflows/ci.yml": b"on: push\nrun: curl evil\n"}, OLD)
    # c2 removes .github again: its tree has only app.py — the final tree looks harmless
    c2, objs2 = _change({"app.py": b"print(1)\n"}, c1)
    resp = _git(proxy, shim, "POST", "/acme/web.git/git-receive-pack", body=_push(OLD, c2, RUN_BRANCH, objs1 + objs2))
    assert resp.status == 403 and up.calls == []


def test_a_commit_reusing_an_existing_tree_with_other_workflows_is_refused(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    # On the remote: an old tree whose workflows differ from the branch tip's (none).
    old_wf_blob = _obj("blob", b"on: pull_request_target\n")
    wf_tree = _obj("tree", _tree([("100644", "ci.yml", old_wf_blob[0])]))
    gh_tree = _obj("tree", _tree([("40000", "workflows", wf_tree[0])]))
    root = _obj("tree", _tree([("40000", ".github", gh_tree[0])]))
    for sha, _, data in (wf_tree, gh_tree, root):
        hub.remote_trees[sha] = [{"mode": m.decode(), "path": n.decode(), "sha": s}
                                 for n, (m, s) in egress_rules._tree_entries(data).items()]
    # The pushed commit carries ONLY itself: its tree is "not in the pack" (the server has it).
    commit = _obj("commit", _commit(root[0], [OLD]))
    resp = _git(proxy, shim, "POST", "/acme/web.git/git-receive-pack", body=_push(OLD, commit[0], RUN_BRANCH, [commit]))
    assert resp.status == 403 and up.calls == []


def _ref_delta(base_sha: str, base: bytes, target: bytes) -> bytes:
    """A delta that rebuilds `target` from `base` by one insert (valid git delta encoding)."""
    def varint(n: int) -> bytes:
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)
    body = varint(len(base)) + varint(len(target))
    for i in range(0, len(target), 127):
        chunk = target[i:i + 127]
        body += bytes([len(chunk)]) + chunk
    return body


def _thin_pack(objs, deltas) -> bytes:
    """objs: (sha, code, data); deltas: (base_sha, delta_bytes)."""
    out = bytearray(b"PACK" + (2).to_bytes(4, "big") + (len(objs) + len(deltas)).to_bytes(4, "big"))

    def header(code: int, size: int) -> bytes:
        byte = (code << 4) | (size & 0x0F)
        size >>= 4
        h = bytearray()
        while size:
            h.append(byte | 0x80)
            byte = size & 0x7F
            size >>= 7
        h.append(byte)
        return bytes(h)
    for _, code, data in objs:
        out += header(code, len(data)) + zlib.compress(data)
    for base_sha, delta in deltas:
        out += header(7, len(delta)) + bytes.fromhex(base_sha) + zlib.compress(delta)
    return bytes(out) + hashlib.sha1(bytes(out)).digest()


def test_a_thin_pack_is_resolved_against_the_remote_and_still_checked(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    # Remote base: a root tree with app.py only (the parent's tree).
    app_v1 = _obj("blob", b"print(1)\n")
    base_root = _obj("tree", _tree([("100644", "app.py", app_v1[0])]))
    hub.remote_trees[base_root[0]] = [{"mode": "100644", "path": "app.py", "sha": app_v1[0]}]
    hub.remote_commits[OLD] = base_root[0]

    def push_with_new_root(entries):
        new_root_data = _tree(entries)
        new_root_sha = _obj("tree", new_root_data)[0]
        commit = _obj("commit", _commit(new_root_sha, [OLD]))
        delta = _ref_delta(base_root[0], base_root[2], new_root_data)
        return commit, _thin_pack([commit], [(base_root[0], delta)])

    # A harmless change, delta'd against the remote's tree: allowed.
    app_v2 = _obj("blob", b"print(2)\n")
    commit, pack = push_with_new_root([("100644", "app.py", app_v2[0])])
    line = f"{OLD} {commit[0]} {RUN_BRANCH}\0 report-status\n".encode()
    body = f"{len(line) + 4:04x}".encode() + line + b"0000" + pack
    assert _git(proxy, shim, "POST", "/acme/web.git/git-receive-pack", body=body).status == 200
    # A workflow brought in through a thin delta of the root tree: refused.
    wf_tree = _obj("tree", _tree([("100644", "ci.yml", app_v2[0])]))
    gh_tree = _obj("tree", _tree([("40000", "workflows", wf_tree[0])]))
    hub.remote_trees[gh_tree[0]] = [{"mode": "40000", "path": "workflows", "sha": wf_tree[0]}]
    commit, pack = push_with_new_root([("40000", ".github", gh_tree[0]), ("100644", "app.py", app_v1[0])])
    line = f"{OLD} {commit[0]} {RUN_BRANCH}\0 report-status\n".encode()
    body = f"{len(line) + 4:04x}".encode() + line + b"0000" + pack
    before = len(up.calls)
    assert _git(proxy, shim, "POST", "/acme/web.git/git-receive-pack", body=body).status == 403
    assert len(up.calls) == before


def test_an_unresolvable_thin_delta_is_refused(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    commit = _obj("commit", _commit("e" * 40, [OLD]))
    pack = _thin_pack([commit], [("f" * 40, _ref_delta("f" * 40, b"x", b"y"))])
    line = f"{OLD} {commit[0]} {RUN_BRANCH}\0 report-status\n".encode()
    body = f"{len(line) + 4:04x}".encode() + line + b"0000" + pack
    assert _git(proxy, shim, "POST", "/acme/web.git/git-receive-pack", body=body).status == 403


def test_a_push_over_the_pack_limit_is_refused_before_parsing(env, monkeypatch):
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    monkeypatch.setattr(egress_rules, "MAX_PUSH_BYTES", 1024)
    new, objs = _change({"app.py": __import__("os").urandom(4096)}, OLD)   # incompressible
    resp = _git(proxy, shim, "POST", "/acme/web.git/git-receive-pack", body=_push(OLD, new, RUN_BRANCH, objs))
    assert resp.status == 403 and up.calls == []


# ---- #16 the credential is bound to the sandbox's network identity ---------------------------------

def test_16_the_egress_credential_works_only_from_its_sandbox(env, monkeypatch):
    monkeypatch.setenv(runs.SANDBOX_ADDRS_ENV, "172.20.0.5")
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    ok = proxy.handle(EgressRequest("GET", "/github-api/repos/acme/web", "",
                                    {"authorization": f"Bearer {shim.egress_credential}"}, peer="172.20.0.5"))
    stolen = proxy.handle(EgressRequest("GET", "/github-api/repos/acme/web", "",
                                        {"authorization": f"Bearer {shim.egress_credential}"}, peer="172.20.0.9"))
    assert ok.status == 200 and stolen.status == 401


def test_16_with_no_known_address_the_first_use_binds_it(env, monkeypatch):
    monkeypatch.delenv(runs.SANDBOX_ADDRS_ENV, raising=False)
    monkeypatch.setattr(runs, "sandbox_network_id", lambda: runs.TOFU)
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    first = proxy.handle(EgressRequest("GET", "/github-api/repos/acme/web", "",
                                       {"authorization": f"Bearer {shim.egress_credential}"}, peer="10.1.1.1"))
    again = proxy.handle(EgressRequest("GET", "/github-api/repos/acme/web", "",
                                       {"authorization": f"Bearer {shim.egress_credential}"}, peer="10.1.1.1"))
    other = proxy.handle(EgressRequest("GET", "/github-api/repos/acme/web", "",
                                       {"authorization": f"Bearer {shim.egress_credential}"}, peer="10.9.9.9"))
    assert (first.status, again.status, other.status) == (200, 200, 401)


def test_round2_git_is_challenged_for_its_credential_and_the_handshake_is_not_audited(env, monkeypatch):
    """The compose walk: git (libcurl) only sends the credential its helper gives after a 401 that
    names a scheme. Egress answered a bare 401, so every clone and push through it failed with
    "Authentication failed". The handshake is challenged (Basic) and not recorded as a denial; a
    WRONG credential is still refused, challenged and audited."""
    hub, g, shim, up, proxy = _setup(env, monkeypatch)
    before = len(env.world.events)
    first = proxy.handle(EgressRequest("GET", "/github/acme/web.git/info/refs", "service=git-upload-pack", {}))
    assert first.status == 401 and first.headers.get("www-authenticate", "").startswith("Basic")
    assert len(env.world.events) == before and up.calls == []
    wrong = proxy.handle(EgressRequest("GET", "/github/acme/web.git/info/refs", "service=git-upload-pack",
                                       {"authorization": _basic("boss_egr_" + "x" * 40)}))
    assert wrong.status == 401 and wrong.headers.get("www-authenticate", "").startswith("Basic")
    assert any(e.event_type == "egress.denied" for e in env.world.events[before:])
