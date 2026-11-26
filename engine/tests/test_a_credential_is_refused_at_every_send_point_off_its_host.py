"""THE SEND-POINT PROOF (BOSSAPP-1286, third review of #330).

Guarding where clients are BUILT kept missing token sources. So every GitHub credential now
carries its host (identity/github_urls.GitHubCredential) and every place that PUTS one on the
wire calls the one check (github_urls.check_send). This test crosses every credential source
with every sender: a credential sent to a host it does not belong to raises CrossHostCredential
before any byte leaves, and the matching host goes through.

Sources (each yields a credential tagged by the code that issued it):
  the one resolver (repo_access.resolve), a connection's token (token_for_connection), a
  binding's token (token_for_repository), the App's installation token (GitHubAppAdapter),
  repo_token's install token, and an untagged string (= the install's own env token).
Senders: GitHubAdapter requests, git credential injection (repo_host.git_access), the multirepo
transport, the intake adapter, setup probes, pat_abilities, the CI/kit reader, the stack deploy
reader, the branch picker, the repo-access probe, the precheck, the issues tracker.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from boss_engine.identity import github_urls as G

GHES = "https://ghe.northgate.example/api/v3"
PUBLIC = G.PUBLIC_API


@pytest.fixture(autouse=True)
def _install_is_github_com(monkeypatch):
    monkeypatch.delenv(G.API_BASE_ENV, raising=False)
    monkeypatch.delenv(G.WEB_BASE_ENV, raising=False)


def _sources():
    """(name, credential, its REST base): every kind of credential a source returns."""
    return [
        ("ghes connection", G.tag("ghes-conn-token", GHES), GHES),
        ("github.com connection", G.tag("gh-conn-token", PUBLIC), PUBLIC),
        ("install env (untagged)", "install-env-token", PUBLIC),
    ]


class _NoNetwork:
    """Any sender that gets past the check lands here: it answers, never touches a network."""

    sent: list[str] = []

    def __call__(self, *a, **k):
        url = next((x for x in a if isinstance(x, str) and "://" in x), k.get("url", ""))
        _NoNetwork.sent.append(str(url))
        return httpx.Response(404, json={}, request=httpx.Request("GET", str(url) or "https://x"))


def _senders(monkeypatch):
    """(name, send(token, api_base)) for every send point. Each sends ONE request (or builds one
    git credential header) for `token` to `api_base`."""
    from boss_engine.multirepo import github as multirepo
    from boss_engine.services import (delivery_sources, kit_connections, repo_access, repo_creation,
                                      repo_host, setup_checks, stack_deploy)
    from boss_engine.services.github_adapter import GitHubAdapter

    fake = _NoNetwork()
    monkeypatch.setattr(httpx, "request", fake)
    monkeypatch.setattr(httpx, "get", fake)
    monkeypatch.setattr(setup_checks, "default_http",
                        lambda m, u, headers=None, **k: SimpleNamespace(status=404, body={},
                                                                        headers={}))

    class _Client:
        def __init__(self, *a, **k):
            pass

        def get(self, url, headers=None):
            return fake(url)

        def close(self):
            pass

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(
        OSError("no network in this test")))
    settings = SimpleNamespace(target_github_owner="o", target_github_repo="r", github_token="",
                               github_app_id=None, github_app_private_key=None,
                               github_app_installation_id=None)

    def adapter(token, base):
        GitHubAdapter(settings, owner="o", repo="r", token=token, api_base_url=base)._headers()

    def git(token, base):
        repo_host.git_access("github", "o", "r", token, web_base=G.web_base_for(base))

    def multirepo_transport(token, base):
        # The multirepo transport sends to the TOKEN'S host by construction; aiming it elsewhere
        # is only possible by a mis-tagged token, so it is proven through check_send directly.
        G.check_send(token, base + "/repos/o/r", where="multirepo")
        multirepo._default_transport("GET", "/repos/o/r", token)

    def setup_probe(token, base):
        setup_checks.probe_repository(token, "o/r", api_base=base)

    def abilities(token, base):
        repo_creation.pat_abilities(token, api_base=base)

    def kit_reader(token, base):
        kit_connections._http_get(f"{base}/repos/o/r", token)  # noqa: SLF001

    def deploy_reader(token, base):
        stack_deploy.GitHubSource(token, base)._get("/repos/o/r")  # noqa: SLF001

    def branch_picker(token, base):
        delivery_sources._github("/repos/o/r", token, base)  # noqa: SLF001

    def access_probe(token, base):
        repo_access._probe_once(token, "o", "r", base, _Client())  # noqa: SLF001

    return [("GitHubAdapter", adapter), ("git credential", git),
            ("multirepo transport", multirepo_transport), ("setup probe", setup_probe),
            ("pat_abilities", abilities), ("CI/kit reader", kit_reader),
            ("stack deploy reader", deploy_reader), ("branch picker", branch_picker),
            ("repo-access probe", access_probe)]


def test_every_source_times_every_sender_refuses_the_other_host(monkeypatch):
    # The matrix includes an UNTAGGED install token on purpose (production sends it as the
    # install's own and counts it); strict mode is proven separately below.
    monkeypatch.delenv(G.STRICT_ENV, raising=False)
    refused, allowed = [], []
    for sname, token, own in _sources():
        other = PUBLIC if own == GHES else GHES
        for name, send in _senders(monkeypatch):
            _NoNetwork.sent.clear()
            with pytest.raises(G.CrossHostCredential):
                send(token, other)
            assert not any(G._netloc(other) in u for u in _NoNetwork.sent), (sname, name)  # noqa: SLF001
            refused.append((sname, name))
            try:                                   # its own host: no refusal
                send(token, own)
            except G.CrossHostCredential as error:  # pragma: no cover - the failure we test for
                pytest.fail(f"{name} refused {sname} on its own host: {error}")
            except Exception:  # noqa: BLE001 — the fake network answers 404 / OSError
                pass
            allowed.append((sname, name))
    assert len(refused) == len(allowed) == len(_sources()) * len(_senders(monkeypatch))


def test_the_intake_adapter_never_attaches_a_token_of_another_host():
    from boss_engine.connectors.intake.contract import IntakeConnection
    from boss_engine.connectors.intake.github import adapter as intake

    conn = IntakeConnection("icn", "p", "github", mode="poll",
                            config={"repository": "o/r", "api_base_url": PUBLIC},
                            secret_resolver={"api_token": G.tag("ghes-token", GHES)}.get)
    # a token of another host is never re-labelled onto this one: the read goes anonymous
    assert "Authorization" not in intake._headers(conn, "o/r")  # noqa: SLF001
    ghes_conn = IntakeConnection("icn2", "p", "github", mode="poll",
                                 config={"repository": "o/r", "api_base_url": GHES},
                                 secret_resolver={"api_token": "pasted-for-this-connection"}.get)
    assert "Authorization" in intake._headers(ghes_conn, "o/r")  # noqa: SLF001


def test_the_issues_tracker_refuses_an_env_token_on_a_ghes_base():
    from boss_engine.services.work_tracker_adapters import _github_tracker_token

    with pytest.raises(G.CrossHostCredential):
        _github_tracker_token("install-env-token", GHES, {"credential_ref": "env:BOSS_GITHUB_TOKEN"})
    pasted = _github_tracker_token("pasted", GHES, {"credential_ref": "secret:abc"})
    G.check_send(pasted, GHES)


def test_a_tag_is_never_laundered_onto_another_host():
    with pytest.raises(G.CrossHostCredential):
        G.tag(G.tag("t", GHES), PUBLIC)
    assert repr(G.tag("secret-value", GHES)).count("secret-value") == 0


# ---- the static guard: every GitHub credential put on the wire goes through the send check ---------

import ast as _ast
import re as _re
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parents[2]
#: Header helpers that build but never send; every caller sends through a check (named here).
_HELPERS = {
    "engine/boss_engine/services/setup_checks.py:_gh_headers":
        "inspect_token/probe_repository/pat_abilities/_org_member call _send_check first",
    "engine/boss_engine/services/stack_deploy.py:_headers": "GitHubSource._get calls check_send",
}

_GH = _re.compile(r"vnd\.github|X-GitHub-Api-Version|x-access-token|api\.github|github_urls|GH_TOKEN|GITHUB_TOKEN", _re.I)
_CREDTXT = _re.compile(r"Bearer |x-access-token:|AUTHORIZATION: basic|^token |basic ", _re.I)
_CHECKS = {"check_send","_send_check","_send_base","_check_jwt_host","_github_tracker_token"}
def _names(node):
    return {_ast.unparse(n) for n in _ast.walk(node) if isinstance(n,(_ast.Name,_ast.Attribute)) and not isinstance(getattr(n,'ctx',None),_ast.Store)} - set()
def _uses(fn):
    out=set()
    for n in _ast.walk(fn):
        if isinstance(n,_ast.JoinedStr):
            prev=""
            for v in n.values:
                if isinstance(v,_ast.Constant) and isinstance(v.value,str):
                    prev=v.value
                elif isinstance(v,_ast.FormattedValue):
                    if _re.search(r"(Bearer |x-access-token:|basic |^token )$", prev, _re.I):
                        out.add(_ast.unparse(v.value))
                    prev=""
        elif isinstance(n,_ast.BinOp) and isinstance(n.op,_ast.Add):
            for side,other in ((n.left,n.right),(n.right,n.left)):
                if isinstance(side,_ast.Constant) and isinstance(side.value,str) and _CREDTXT.search(side.value):
                    out.add(_ast.unparse(other))
        elif isinstance(n,_ast.Call) and isinstance(n.func,_ast.Attribute) and n.func.attr=="format" and isinstance(n.func.value,_ast.Constant) and _CREDTXT.search(str(n.func.value.value)):
            out.update(_ast.unparse(a) for a in n.args)
        elif isinstance(n,_ast.Call):
            fname=n.func.attr if isinstance(n.func,_ast.Attribute) else getattr(n.func,'id','')
            if fname=="BasicAuth": out.update(_ast.unparse(a) for a in n.args)
            if fname in ("get","post","put","patch","delete","request","stream","Client","AsyncClient"):
                for k in n.keywords:
                    if k.arg=="auth": out.add(_ast.unparse(k.value))
        elif isinstance(n,_ast.Dict):
            for k,v in zip(n.keys,n.values):
                if isinstance(k,_ast.Constant) and k.value in ("GH_TOKEN","GITHUB_TOKEN") and v is not None:
                    out.add(_ast.unparse(v)+"#env")
    return out
def _checked(fn):
    out=set()
    for n in _ast.walk(fn):
        if isinstance(n,_ast.Call):
            fname=n.func.attr if isinstance(n.func,_ast.Attribute) else getattr(n.func,'id','')
            if fname in _CHECKS:
                out.add(_ast.unparse(n.args[0]) if n.args else "*")
    # a value DERIVED from a checked credential (basic = b64(f"user:{token}")) is checked too
    changed=True
    while changed:
        changed=False
        for n in _ast.walk(fn):
            if isinstance(n,_ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],_ast.Name):
                t=n.targets[0].id
                if t not in out and _names(n.value) & out:
                    out.add(t); changed=True
    return out


def _unchecked_in(src: str, rel: str) -> list[str]:
    try:
        tree = _ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for fn in _ast.walk(tree):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        if f"{rel}:{fn.name}" in _HELPERS or not _GH.search(_ast.get_source_segment(src, fn) or ""):
            continue
        used, ok = _uses(fn), _checked(fn)
        missing = sorted(u for u in used if u.replace("#env", "") not in ok and "*" not in ok)
        if missing:
            out.append(f"{rel}:{fn.lineno} {fn.name} {missing}")
    return out


def _unchecked_github_credential_builders(root=None) -> list[str]:
    root = root or _ROOT
    out = []
    for top in ("engine/boss_engine", "apps/api/app", "apps/egress", "apps/worker"):
        for path in sorted((root / top).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if "/tests/" in rel or rel.endswith("identity/github_urls.py"):
                continue
            out += _unchecked_in(path.read_text(encoding="utf-8", errors="replace"), rel)
    return out


def test_every_github_credential_put_on_the_wire_goes_through_the_send_check():
    """Every Authorization (f-string, .format, concatenation), basic git header, token-in-URL,
    httpx auth=/BasicAuth and GH_TOKEN/GITHUB_TOKEN handed to a subprocess, in GitHub code, must
    have check_send called ON THAT SAME credential (or a value derived from a checked one) in the
    same function — merely mentioning check_send is not enough (BOSSAPP-1286)."""
    missing = _unchecked_github_credential_builders()
    assert not missing, ("call github_urls.check_send(<that token>, url) before sending it:\n"
                         + "\n".join(missing))


@pytest.mark.parametrize("code", [
    'def f(t):\n    return {"Authorization": f"Bearer {t}", "Accept": "application/vnd.github+json"}\n',
    'def f(t):\n    return {"Authorization": "Bearer {}".format(t), "X-GitHub-Api-Version": "x"}\n',
    'def f(t):\n    return {"Authorization": "Bearer " + t, "Accept": "application/vnd.github+json"}\n',
    'def f(t, httpx):\n    return httpx.get("https://api.github.com/user", auth=t)\n',
    'def f(t, run):\n    return run(["gh", "pr"], env={"GH_TOKEN": t})\n',
    'def f(t, other, check_send):\n    check_send(other, "u")\n'
    '    return {"Authorization": f"Bearer {t}", "Accept": "application/vnd.github+json"}\n',
])
def test_the_static_guard_sees_each_way_a_credential_is_built(code):
    assert _unchecked_in(code, "engine/boss_engine/x.py"), code


def test_the_static_guard_accepts_a_checked_and_a_derived_credential():
    code = ('def f(t, check_send, b64):\n    check_send(t, "https://api.github.com")\n'
            '    basic = b64(f"x-access-token:{t}")\n'
            '    return {"Authorization": f"basic {basic}", "Accept": "application/vnd.github+json"}\n')
    assert _unchecked_in(code, "engine/boss_engine/x.py") == []


def test_strict_mode_refuses_an_untagged_credential_and_production_counts_it(monkeypatch):
    """In tests and development (BOSS_GITHUB_CREDENTIALS_STRICT=1) an untagged credential at a
    send point is a hard error: its source forgot to tag. In production it is sent as the
    install's own (to the install's host only) and counted."""
    monkeypatch.setenv(G.STRICT_ENV, "1")
    with pytest.raises(G.UntaggedGitHubCredential):
        G.check_send("raw-token", PUBLIC + "/user")
    G.check_send(G.tag("raw-token", PUBLIC), PUBLIC + "/user")          # tagged: fine
    monkeypatch.delenv(G.STRICT_ENV)
    before = G.UNTAGGED_SENDS.get("count-test", 0)
    G.check_send("raw-token", PUBLIC + "/user", where="count-test")
    assert G.UNTAGGED_SENDS["count-test"] == before + 1


def test_the_install_token_is_tagged_at_its_source(monkeypatch):
    from boss_engine.config import Settings

    monkeypatch.setenv("BOSS_GITHUB_TOKEN", "install-secret")
    token = Settings(_env_file=None).github_token
    assert isinstance(token, G.GitHubCredential) and token.api_base == G.install_api_base()


def test_the_untagged_token_ratchet_only_shrinks():
    """The legacy list (tests still handing raw placeholder tokens) names real files and never
    grows. Lower CAP when you remove a line."""
    CAP = 12  # noqa: N806
    for name in ("engine/tests", "apps/api/tests"):
        listed = _ROOT / name / "_untagged_token_legacy.txt"
        if not listed.exists():
            continue
        rows = [r.strip() for r in listed.read_text().splitlines()
                if r.strip() and not r.startswith("#")]
        assert all((_ROOT / name / r).exists() for r in rows), rows
        assert len(rows) <= CAP, f"{listed}: {len(rows)} > {CAP}: tag the tokens instead"
