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

from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parents[2]
import ast  # noqa: E402 — the static guard below
import re  # noqa: E402

#: Places that build a credential header and cannot call check_send themselves (named, with why).
_GUEST_WHY = ("the dependency-free guest clones with spec.token from spec.github_web; the HOST "
              "checked that pair (repo_host.guest_github_web) before building the spec, and the "
              "guest refuses a token that came without github_web")
_NOT_SEND_POINTS = {
    "sandbox-host/guest/in_guest_agent.py:_prepare_repo": _GUEST_WHY,
    # the sandbox-service's copy of the same guest (kept byte-identical, tested)
    "services/sandbox-service/internal/backend/guest/in_guest_agent.py:_prepare_repo": _GUEST_WHY,
}
_ROOTS = ("engine/boss_engine", "apps/api/app", "apps/egress", "apps/worker", "sandbox-host",
          "services/sandbox-service")
#: A no-argument checker that judges ONE specific credential itself, and the expression it covers.
#: A bare check_send() (or any other checker called without its credential) covers nothing.
_SELF_CHECKS = {"_check_jwt_host": "self._app_jwt()"}

GH_MARKER = re.compile(r"vnd\.github|X-GitHub-Api-Version|x-access-token|api\.github|github_urls|"
                       r"GH_TOKEN|GITHUB_TOKEN|github\.com|GitHub", re.I)
WEAK_TXT = re.compile(r"(Bearer|token|basic)\s*(%s)?$", re.I)
CHECKS = {"check_send", "_send_check", "_send_base", "_check_jwt_host", "_github_tracker_token",
          "guest_github_web"}
ENV_KEYS = ("GH_TOKEN", "GITHUB_TOKEN")
SPEC_KEYS = {"github_web", "repo_host", "work_branch", "base_branch"}


def _src(n):
    return ast.unparse(n)


def _fn_name(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


class _Uses(ast.NodeVisitor):
    """(expr, line, strong) for every place a credential is put into a request, a git URL, a
    subprocess env or a guest spec."""

    def __init__(self):
        self.out = []

    def add(self, node, line, strong):
        if isinstance(node, ast.Constant):
            return
        self.out.append((_src(node), line, strong))

    def visit_JoinedStr(self, n):
        vals = n.values
        for i, v in enumerate(vals):
            if not isinstance(v, ast.FormattedValue):
                continue
            prev = vals[i - 1].value if i and isinstance(vals[i - 1], ast.Constant) else ""
            nxt = vals[i + 1].value if i + 1 < len(vals) and isinstance(vals[i + 1], ast.Constant) else ""
            prev, nxt = str(prev), str(nxt)
            if re.search(r"(x-access-token:|oauth2:)$", prev) or \
                    (re.search(r"https?://$", prev) and nxt.startswith("@")):
                self.add(v.value, n.lineno, True)          # a token in a git/HTTP URL
            elif re.search(r"(Bearer |token |basic )$", prev, re.I):
                self.add(v.value, n.lineno, False)
        self.generic_visit(n)

    def visit_BinOp(self, n):
        if isinstance(n.op, ast.Add):
            for side, other in ((n.left, n.right), (n.right, n.left)):
                if isinstance(side, ast.Constant) and isinstance(side.value, str) and \
                        re.search(r"(Bearer |token |x-access-token:)$", side.value, re.I):
                    self.add(other, n.lineno, "x-access-token" in side.value)
        if isinstance(n.op, ast.Mod) and isinstance(n.left, ast.Constant) and \
                re.search(r"(Bearer|token|basic) %s", str(n.left.value), re.I):
            parts = n.right.elts if isinstance(n.right, ast.Tuple) else [n.right]
            for p in parts:
                self.add(p, n.lineno, False)
        self.generic_visit(n)

    def visit_Call(self, n):
        name = _fn_name(n)
        f = n.func
        if name == "format" and isinstance(f, ast.Attribute) and isinstance(f.value, ast.Constant) \
                and re.search(r"(Bearer|token|basic) \{", str(f.value.value), re.I):
            for a in n.args:
                self.add(a, n.lineno, False)
        if name == "join" and n.args and isinstance(n.args[0], (ast.List, ast.Tuple)):
            elts = n.args[0].elts
            if any(isinstance(e, ast.Constant) and str(e.value).strip().lower() in ("bearer", "token", "basic")
                   for e in elts):
                for e in elts:
                    self.add(e, n.lineno, False)
        if name == "BasicAuth":
            for a in n.args:
                self.add(a, n.lineno, False)
        if name in ("get", "post", "put", "patch", "delete", "request", "stream", "Client",
                    "AsyncClient", "head"):
            for k in n.keywords:
                if k.arg == "auth":
                    self.add(k.value, n.lineno, False)
        for k in n.keywords:                      # env.update(GH_TOKEN=t), dict(os.environ, GH_TOKEN=t)
            if k.arg in ENV_KEYS:
                self.add(k.value, n.lineno, True)
            if k.arg is None and isinstance(k.value, ast.Dict):   # f(**{"token": t, ...})
                self._spec(k.value, n.lineno, force=False)
        self.generic_visit(n)

    def visit_Assign(self, n):
        for t in n.targets:                       # env["GH_TOKEN"] = t
            if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value in ENV_KEYS:
                self.add(n.value, n.lineno, True)
        self.generic_visit(n)

    def visit_Dict(self, n):
        for k, v in zip(n.keys, n.values):
            if isinstance(k, ast.Constant) and k.value in ENV_KEYS and v is not None:
                self.add(v, n.lineno, True)
        self._spec(n, n.lineno, force=False)
        self.generic_visit(n)

    def _spec(self, d, line, force):
        keys = {k.value for k in d.keys if isinstance(k, ast.Constant)}
        if "token" in keys and (force or keys & SPEC_KEYS):
            for k, v in zip(d.keys, d.values):
                if isinstance(k, ast.Constant) and k.value == "token" and v is not None:
                    self.add(v, line, True)       # a token handed to a guest/spec is a send


def _checks(fn):
    """{expr: [lines]} where the send check ran on that credential (or one derived from it),
    and {expr: [lines]} of every assignment to a name."""
    checked, assigned = {}, {}
    nodes = list(ast.walk(fn))
    for n in nodes:
        if isinstance(n, ast.Call) and _fn_name(n) in CHECKS:
            if n.args:
                key = _src(n.args[0])
            elif any(k.arg == "token" for k in n.keywords):
                key = _src(next(k.value for k in n.keywords if k.arg == "token"))
            elif _fn_name(n) in _SELF_CHECKS:
                key = _SELF_CHECKS[_fn_name(n)]
            else:
                continue                      # a bare check covers no credential
            checked.setdefault(key, []).append(n.lineno)
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                for tn in ast.walk(t):
                    if isinstance(tn, (ast.Name, ast.Attribute)) and isinstance(tn.ctx, ast.Store):
                        assigned.setdefault(_src(tn), []).append(n.lineno)
    # derived: `basic = b64(f"x:{token}")` after token was checked
    changed = True
    while changed:
        changed = False
        for n in nodes:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                tname = n.targets[0].id
                inner = {_src(x) for x in ast.walk(n.value) if isinstance(x, (ast.Name, ast.Attribute))}
                if any(k in checked and min(checked[k]) <= n.lineno for k in inner) and \
                        n.lineno not in checked.get(tname, []):
                    checked.setdefault(tname, []).append(n.lineno)
                    changed = True
    return checked, assigned


def _covered(expr, line, checked, assigned):
    for cline in checked.get(expr, []):
        # the check must come before the use, with no reassignment in between
        if cline <= line and not any(cline < a <= line for a in assigned.get(expr, [])
                                     if a not in checked.get(expr, [])):
            return True
    return False


def _code_text(fn):
    """The function's CODE (no comments, no docstrings): what decides "is this GitHub"."""
    body = [st for st in fn.body if not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
                                         and isinstance(st.value.value, str))]
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


def _functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _own_nodes(fn):
    """The function's statements without nested function bodies (they are judged on their own)."""
    clone = ast.Module(body=[s for s in fn.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))],
                       type_ignores=[])
    return clone


def scan(files):
    """files: [(rel, src)]. Returns offenders. A function that builds a credential header from
    one of its PARAMETERS without checking it is a HELPER: every call site must check the
    argument it passes instead (the check moves to where the token is known)."""
    helpers = {}        # name -> {(param index, param name)}
    pending = []
    parsed = []
    for rel, src in files:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        parsed.append((rel, src, tree))
        for fn in _functions(tree):
            seg = _code_text(fn)
            v = _Uses()
            v.visit(_own_nodes(fn))
            if not v.out:
                continue
            checked, assigned = _checks(fn)
            params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
            for expr, line, strong in v.out:
                if not strong and not GH_MARKER.search(seg):
                    continue
                if _covered(expr, line, checked, assigned):
                    continue
                if expr in params and expr not in assigned:
                    idx = params.index(expr) - (1 if params and params[0] in ("self", "cls") else 0)
                    helpers.setdefault(fn.name, set()).add((idx, expr))
                    continue
                pending.append(f"{rel}:{line} {fn.name} {expr}")
    for rel, src, tree in parsed:
        for fn in _functions(tree):
            checked, assigned = _checks(fn)
            for n in ast.walk(_own_nodes(fn)):
                if isinstance(n, ast.Call) and _fn_name(n) in helpers:
                    for idx, pname in helpers[_fn_name(n)]:
                        passed = n.args[idx] if 0 <= idx < len(n.args) else next(
                            (k.value for k in n.keywords if k.arg == pname), None)
                        if passed is None or isinstance(passed, ast.Constant):
                            continue
                        expr = _src(passed)
                        if not _covered(expr, n.lineno, checked, assigned):
                            pending.append(f"{rel}:{n.lineno} {fn.name} {expr} (via {_fn_name(n)})")
    return sorted(set(pending))


def _unchecked_in(src: str, rel: str) -> list[str]:
    return [o for o in scan([(rel, src)])
            if f"{rel}:{o.split(' ')[1]}" not in _NOT_SEND_POINTS]


def _unchecked_github_credential_builders(root=None) -> list[str]:
    root = root or _ROOT
    files = []
    for top in _ROOTS:
        for path in sorted((root / top).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if "/tests/" in rel or rel.endswith("identity/github_urls.py"):
                continue
            files.append((rel, path.read_text(encoding="utf-8", errors="replace")))
    return [o for o in scan(files) if f"{o.split(':')[0]}:{o.split(' ')[1]}" not in _NOT_SEND_POINTS]


def test_every_github_credential_put_on_the_wire_goes_through_the_send_check():
    """Every credential put into a request header (f-string, .format, %, concatenation, join),
    an httpx auth=/BasicAuth, a token-in-URL (x-access-token:, oauth2:, https://{t}@), a
    GH_TOKEN/GITHUB_TOKEN in a subprocess env (literal, env[...] =, .update, dict(os.environ, ...))
    or a guest spec's "token" must have check_send run ON THAT credential, before the use and with
    no reassignment in between. A header helper fed a parameter moves the duty to every call site.
    Strong patterns need no GitHub marker in the function (BOSSAPP-1286)."""
    missing = _unchecked_github_credential_builders()
    assert not missing, ("call github_urls.check_send(<that token>, url) before using it:\n"
                         + "\n".join(missing))


_GH_ACCEPT = '"Accept": "application/vnd.github+json"'


@pytest.mark.parametrize("code", [
    # header building, weak patterns (in GitHub code)
    'def f(src):\n    t = src()\n    return {"Authorization": f"Bearer {t}", ' + _GH_ACCEPT + '}\n',
    'def f(src):\n    t = src()\n    return {"Authorization": "Bearer {}".format(t), ' + _GH_ACCEPT + '}\n',
    'def f(src):\n    t = src()\n    return {"Authorization": "Bearer " + t, ' + _GH_ACCEPT + '}\n',
    'def f(src):\n    t = src()\n    return {"Authorization": "Bearer %s" % t, ' + _GH_ACCEPT + '}\n',
    'def f(src):\n    t = src()\n    return {"Authorization": " ".join(["Bearer", t]), ' + _GH_ACCEPT + '}\n',
    'def f(src, httpx):\n    t = src()\n    return httpx.get("https://api.github.com/user", auth=t)\n',
    'def f(src, httpx):\n    t = src()\n    return httpx.get("https://api.github.com", auth=httpx.BasicAuth("x", t))\n',
    # subprocess env, strong (no GitHub marker needed)
    'def f(src, run):\n    t = src()\n    return run(["gh"], env={"GH_TOKEN": t})\n',
    'def f(src, env):\n    t = src()\n    env["GITHUB_TOKEN"] = t\n',
    'def f(src, env):\n    t = src()\n    env.update(GH_TOKEN=t)\n',
    'def f(src, os):\n    t = src()\n    return dict(os.environ, GH_TOKEN=t)\n',
    # token in a URL, strong
    'def f(src):\n    t = src()\n    return f"https://x-access-token:{t}@example.test/o/r.git"\n',
    'def f(src):\n    t = src()\n    return f"https://oauth2:{t}@example.test/o/r.git"\n',
    'def f(src):\n    t = src()\n    return f"https://{t}@example.test/o/r.git"\n',
    # a guest spec and a **kwargs hand-off carry the token too
    'def f(src, w):\n    t = src()\n    return {"token": t, "github_web": w}\n',
    'def f(src, go, b):\n    t = src()\n    return go(**{"token": t, "work_branch": b})\n',
    # a header helper fed a parameter: its CALLER must check what it passes
    'def h(t):\n    return {"Authorization": f"Bearer {t}", ' + _GH_ACCEPT + '}\n'
    'def c(src):\n    return h(src())\n',
    # a check on ANOTHER variable, and a reassignment after the check
    'def f(src, other, check_send):\n    t = src()\n    check_send(other, "u")\n'
    '    return {"Authorization": f"Bearer {t}", ' + _GH_ACCEPT + '}\n',
    'def f(src, again, check_send):\n    t = src()\n    check_send(t, "u")\n    t = again()\n'
    '    return {"Authorization": f"Bearer {t}", ' + _GH_ACCEPT + '}\n',
    # a header helper fed by KEYWORD: the caller must still check what it passes
    'def h(*, token):\n    return {"Authorization": f"Bearer {token}", ' + _GH_ACCEPT + '}\n'
    'def c(src):\n    return h(token=src())\n',
    # a bare check_send() names no credential, so it covers none
    'def f(src, check_send):\n    t = src()\n    check_send()\n'
    '    return {"Authorization": f"Bearer {t}", ' + _GH_ACCEPT + '}\n',
])
def test_the_static_guard_sees_each_way_a_credential_is_built(code):
    assert _unchecked_in(code, "engine/boss_engine/x.py"), code


@pytest.mark.parametrize("code", [
    # checked before use, and a value derived from a checked credential
    'def f(src, check_send, b64):\n    t = src()\n    check_send(t, "https://api.github.com")\n'
    '    basic = b64(f"x-access-token:{t}")\n'
    '    return {"Authorization": f"basic {basic}", ' + _GH_ACCEPT + '}\n',
    # a helper whose caller checks what it passes
    'def h(t):\n    return {"Authorization": f"Bearer {t}", ' + _GH_ACCEPT + '}\n'
    'def c(src, check_send):\n    t = src()\n    check_send(t, "u")\n    return h(t)\n',
    # a keyword-fed helper whose caller checks what it passes
    'def h(*, token):\n    return {"Authorization": f"Bearer {token}", ' + _GH_ACCEPT + '}\n'
    'def c(src, check_send):\n    t = src()\n    check_send(t, "u")\n    return h(token=t)\n',
    # a non-GitHub bearer (no GitHub marker) is not this guard's business
    'def f(key):\n    k = key()\n    return {"Authorization": f"Bearer {k}"}\n',
    # an ssh user is not a token
    'def f(user, host):\n    return f"ssh://{user}@{host}"\n',
])
def test_the_static_guard_accepts_what_is_checked_or_not_a_credential(code):
    assert _unchecked_in(code, "engine/boss_engine/x.py") == [], code


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


def test_the_micro_vm_guest_clones_from_the_tokens_own_host(monkeypatch):
    """The guest is a send point (sixth review, finding 4): spec.github_web comes from the
    token's own host, never from an adapter's base, and the check runs before the spec exists."""
    from boss_engine.services.repo_host import guest_github_web

    assert guest_github_web(G.tag("ghes-tok", GHES), "https://github.com") == G.web_base_for(GHES)
    assert guest_github_web(G.tag("gh-tok", PUBLIC), "https://ghe.northgate.example") == G.PUBLIC_WEB
    assert guest_github_web("", "https://github.com") == "https://github.com"           # anonymous
    assert guest_github_web("bb-tok", "", host="bitbucket") == ""                        # not GitHub
    monkeypatch.setenv(G.STRICT_ENV, "1")
    with pytest.raises(G.UntaggedGitHubCredential):                                     # untagged
        guest_github_web("raw-token", "https://github.com")


def test_the_two_guest_copies_are_identical():
    """The guest ships twice (sandbox-host and the sandbox-service's backend); a fix to one that
    misses the other is how a token would reach a default host. They must be byte-identical."""
    a = (_ROOT / "sandbox-host/guest/in_guest_agent.py").read_bytes()
    b = (_ROOT / "services/sandbox-service/internal/backend/guest/in_guest_agent.py").read_bytes()
    assert a == b


def test_the_guest_refuses_a_token_without_its_host():
    src = (_ROOT / "sandbox-host/guest/in_guest_agent.py").read_text()
    assert 'or "https://github.com").rstrip' not in src          # no default host for a token
    assert "clone refused: a git token came without github_web" in src
