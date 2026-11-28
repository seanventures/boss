"""Authenticated package registries (npm, PyPI, Maven) via boss-egress (BOSSAPP-1418).

Sandboxes talk plain HTTP to http://egress:8090/{npm,pypi,maven}/; boss-egress loads
credentials from the secret store or egress-only env vars, strips inbound credentials, rewrites
packument/index links back through egress, and strips Authorization on cross-origin redirects.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.egress.boss_egress import app as egress_app, registry as egress_registry  # noqa: E402
from apps.egress.boss_egress.proxy import EgressRequest, EgressResponse  # noqa: E402
from boss_engine.identity import egress_rules  # noqa: E402
from boss_engine.services import enterprise_network as en, secret_backends, toolchains  # noqa: E402
from boss_engine.services.agent_env import agent_environment  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry_env(monkeypatch):
    for var in (
        "BOSS_EGRESS_URL",
        "BOSS_NPM_REGISTRY", "BOSS_NPM_CREDENTIAL_REF", "BOSS_NPM_TOKEN", "BOSS_NPM_USERNAME", "BOSS_NPM_PASSWORD",
        "BOSS_PIP_INDEX_URL", "BOSS_PIP_CREDENTIAL_REF", "BOSS_PIP_TOKEN", "BOSS_PIP_USERNAME", "BOSS_PIP_PASSWORD",
        "BOSS_MAVEN_MIRROR", "BOSS_MAVEN_CREDENTIAL_REF", "BOSS_MAVEN_TOKEN", "BOSS_MAVEN_USERNAME", "BOSS_MAVEN_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)


def _sqlite_secret_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'secrets.db'}")
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE secret_refs ("
            "id TEXT PRIMARY KEY, project_id TEXT, scope_type TEXT, scope_id TEXT, "
            "ref_kind TEXT, ref_key TEXT, secret_kind TEXT, storage_backend TEXT, "
            "status TEXT, provider TEXT, metadata TEXT, created_by TEXT)"
        ))
        c.execute(text(
            "CREATE TABLE secret_material ("
            "ref_key TEXT PRIMARY KEY, ciphertext BLOB, updated_at TEXT)"
        ))
        c.execute(text(
            "CREATE TABLE secret_access_events ("
            "id TEXT PRIMARY KEY, secret_ref_id TEXT, project_id TEXT, outcome TEXT, "
            "principal TEXT, reason TEXT, created_at TEXT)"
        ))
    return engine


def test_canonical_registry_path_allows_scoped_npm_and_blocks_traversal():
    dec, enc = egress_registry.canonical_registry_path("/@acme%2fwidget", "npm")
    assert dec == "/@acme/widget"
    assert enc == "/@acme%2fwidget"

    dec2, enc2 = egress_registry.canonical_registry_path("/@acme%2Fwidget/-/widget-1.2.3.tgz", "npm")
    assert dec2 == "/@acme/widget/-/widget-1.2.3.tgz"
    assert enc2 == "/@acme%2Fwidget/-/widget-1.2.3.tgz"

    assert egress_registry.canonical_registry_path("/simple/requests/", "pypi") == (
        "/simple/requests/", "/simple/requests/",
    )

    for bad_kind, bad_path in (
        ("npm", "/../etc/passwd"),
        ("npm", "/pkg/%2e%2e/secret"),
        ("npm", "/pkg/a%5cb"),
        ("npm", "/pkg//tarball.tgz"),
        ("npm", "/pkg%2fother"),               # %2f allowed ONLY in @scope%2fpkg
        ("pypi", "/simple/acme%2fpkg/"),        # %2f refused for pypi
        ("maven", "/org%2facme/artifact.jar"),  # %2f refused for maven
    ):
        with pytest.raises(egress_rules.PathRefused):
            egress_registry.canonical_registry_path(bad_path, bad_kind)


def test_npm_reverse_proxy_injects_secret_store_credential_and_rewrites_tarballs(tmp_path, monkeypatch):
    engine = _sqlite_secret_engine(tmp_path)
    secret_token = "npm_secret_from_db_store_123"
    key = b"k" * 32
    backend = secret_backends.DatabaseBackend(engine=engine, master_key=key)
    with engine.begin() as c:
        ref_id = backend.store(c, "proj_1", secret_token, secret_kind="registry_token")
    monkeypatch.setattr(secret_backends, "backend_for", lambda kind, settings: backend)
    monkeypatch.setenv("BOSS_NPM_REGISTRY", "https://artifacts.example.com/artifactory/api/npm/npm-virtual/")
    monkeypatch.setenv("BOSS_NPM_CREDENTIAL_REF", f"secret_ref:{ref_id}")

    seen = []

    def fake_upstream(method, url, headers, body):
        seen.append((method, url, dict(headers)))
        payload = {
            "name": "@acme/widget",
            "versions": {
                "1.0.0": {
                    "dist": {
                        "tarball": "https://artifacts.example.com/artifactory/api/npm/npm-virtual/@acme/widget/-/widget-1.0.0.tgz",
                    },
                },
                "1.0.1": {
                    "dist": {
                        "tarball": "https://artifacts.example.com/artifactory/npm-local/@acme/widget/-/widget-1.0.1.tgz",
                    },
                },
                "1.0.2": {
                    "dist": {
                        "tarball": "https://cdn.other-host.example/widget-1.0.2.tgz",
                    },
                },
            },
        }
        return EgressResponse(
            200,
            {
                "content-type": "application/json",
                "set-cookie": "SESSION=leak",
                "www-authenticate": "Bearer realm=upstream",
            },
            json.dumps(payload).encode("utf-8"),
        )

    app = egress_app.build_app(engine=engine, registry_upstream=fake_upstream)
    client = TestClient(app)
    resp = client.get(
        "/npm/@acme%2fwidget",
        headers={
            "Authorization": "Bearer sandbox_should_be_stripped",
            "Cookie": "session=bad",
            "X-Api-Key": "leak",
            "Accept": "application/vnd.npm.install-v1+json",
        },
    )
    assert resp.status_code == 200
    assert seen[0][0] == "GET"
    assert seen[0][1] == "https://artifacts.example.com/artifactory/api/npm/npm-virtual/@acme%2fwidget"
    assert seen[0][2]["Authorization"] == f"Bearer {secret_token}"
    assert "cookie" not in {k.lower() for k in seen[0][2]}
    assert "x-api-key" not in {k.lower() for k in seen[0][2]}
    assert "set-cookie" not in {k.lower() for k in resp.headers}
    assert "www-authenticate" not in {k.lower() for k in resp.headers}

    body = resp.json()
    assert body["versions"]["1.0.0"]["dist"]["tarball"] == (
        "http://testserver/npm/@acme/widget/-/widget-1.0.0.tgz"
    )
    assert body["versions"]["1.0.1"]["dist"]["tarball"] == (
        "http://testserver/npm/_origin/artifactory/npm-local/@acme/widget/-/widget-1.0.1.tgz"
    )
    assert body["versions"]["1.0.2"]["dist"]["tarball"] == "https://cdn.other-host.example/widget-1.0.2.tgz"

    # Fetching the rewritten _origin path stays pinned to the configured upstream origin.
    resp2 = client.get("/npm/_origin/artifactory/npm-local/@acme/widget/-/widget-1.0.1.tgz")
    assert resp2.status_code == 200
    assert seen[1][1] == "https://artifacts.example.com/artifactory/npm-local/@acme/widget/-/widget-1.0.1.tgz"
    assert seen[1][2]["Authorization"] == f"Bearer {secret_token}"


def test_pypi_reverse_proxy_rewrites_html_and_maven_uses_store_key(tmp_path, monkeypatch):
    engine = _sqlite_secret_engine(tmp_path)
    monkeypatch.setenv("BOSS_PIP_INDEX_URL", "https://pypi.example.com/artifactory/api/pypi/py-virtual/simple")
    monkeypatch.setenv("BOSS_PIP_USERNAME", "ci-bot")
    monkeypatch.setenv("BOSS_PIP_PASSWORD", "pip-secret-pw")

    monkeypatch.setenv("BOSS_MAVEN_MIRROR", "https://maven.example.com/repo/all")
    monkeypatch.setenv("BOSS_MAVEN_CREDENTIAL_REF", "store:boss/registry/maven")
    monkeypatch.setattr(
        egress_registry,
        "_read_store_key",
        lambda key, db: "maven-store-token-xyz" if key == "boss/registry/maven" else None,
    )

    seen = []

    def fake_upstream(method, url, headers, body):
        seen.append((method, url, dict(headers)))
        if "pypi.example.com" in url:
            html_doc = (
                '<html><body>'
                '<a href="https://pypi.example.com/artifactory/api/pypi/py-virtual/simple/pkg/pkg-1.0.tar.gz">rel</a>'
                '<a href="/artifactory/api/pypi/packages/pkg-1.0-py3-none-any.whl#sha256=abc">whl</a>'
                '<a href="https://files.external-cdn.example/other.whl">ext</a>'
                '</body></html>'
            ).encode("utf-8")
            return EgressResponse(200, {"content-type": "text/html"}, html_doc)
        return EgressResponse(200, {"content-type": "application/java-archive"}, b"JARBYTES")

    app = egress_app.build_app(engine=engine, registry_upstream=fake_upstream)
    client = TestClient(app)

    pypi_resp = client.get("/pypi/pkg/")
    assert pypi_resp.status_code == 200
    expected_basic = "Basic " + base64.b64encode(b"ci-bot:pip-secret-pw").decode("ascii")
    assert seen[0][2]["Authorization"] == expected_basic
    text_body = pypi_resp.text
    assert 'href="http://testserver/pypi/pkg/pkg-1.0.tar.gz"' in text_body
    assert 'href="http://testserver/pypi/_origin/artifactory/api/pypi/packages/pkg-1.0-py3-none-any.whl#sha256=abc"' in text_body
    assert 'href="https://files.external-cdn.example/other.whl"' in text_body

    mvn_resp = client.get("/maven/org/example/app/1.0/app-1.0.jar")
    assert mvn_resp.status_code == 200 and mvn_resp.content == b"JARBYTES"
    assert seen[1][1] == "https://maven.example.com/repo/all/org/example/app/1.0/app-1.0.jar"
    assert seen[1][2]["Authorization"] == "Bearer maven-store-token-xyz"


def test_registry_proxy_hardening_methods_headers_and_cross_origin_redirects(monkeypatch):
    monkeypatch.setenv("BOSS_NPM_REGISTRY", "https://artifacts.example.com/npm/")
    monkeypatch.setenv("BOSS_NPM_TOKEN", "top-secret-npm-token")

    calls = []

    def redirect_upstream(method, url, headers, body):
        calls.append((method, url, dict(headers)))
        if url.startswith("https://artifacts.example.com/npm/same-hop"):
            return EgressResponse(302, {"location": "https://artifacts.example.com/npm/final-same"})
        if url.startswith("https://artifacts.example.com/npm/cross-hop"):
            return EgressResponse(302, {"location": "https://cdn.third-party.example/pkg.tgz"})
        return EgressResponse(200, {"content-type": "application/octet-stream"}, b"ok")

    proxy = egress_registry.RegistryProxy(upstream=redirect_upstream)
    assert proxy.handle("npm", EgressRequest("POST", "/npm/pkg")).status == 405
    assert proxy.handle("npm", EgressRequest("PUT", "/npm/pkg")).status == 405
    assert proxy.handle("npm", EgressRequest("DELETE", "/npm/pkg")).status == 405
    assert proxy.handle(
        "npm",
        EgressRequest("GET", "/npm/pkg", headers={"x-http-method-override": "DELETE"}),
    ).status == 403
    mixed_case_override = proxy.handle(
        "npm",
        EgressRequest("GET", "/npm/pkg", headers={"X-HTTP-Method-Override": "POST"}),
    )
    assert mixed_case_override.status == 403
    assert json.loads(mixed_case_override.body.decode("utf-8")) == {"error": "method_override"}
    assert proxy.handle("npm", EgressRequest("GET", "/npm/../secret")).status == 403
    assert proxy.handle("npm", EgressRequest("GET", "/npm/%2e%2e/secret")).status == 403

    # Same-origin redirect keeps Authorization.
    calls.clear()
    r_same = proxy.handle("npm", EgressRequest("GET", "/npm/same-hop"))
    assert r_same.status == 200
    assert len(calls) == 2
    assert calls[0][2]["Authorization"] == "Bearer top-secret-npm-token"
    assert calls[1][2]["Authorization"] == "Bearer top-secret-npm-token"

    # Cross-origin redirect MUST strip Authorization.
    calls.clear()
    r_cross = proxy.handle("npm", EgressRequest("GET", "/npm/cross-hop"))
    assert r_cross.status == 200
    assert len(calls) == 2
    assert calls[0][2]["Authorization"] == "Bearer top-secret-npm-token"
    assert "Authorization" not in calls[1][2]


def test_sandbox_routes_to_egress_without_seeing_registry_credentials(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"java": {"jdk": "21", "maven": "3.9", "gradle": "8.14"}}))
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))

    source = {
        "BOSS_EGRESS_URL": "http://egress:8090",
        "BOSS_NPM_REGISTRY": "https://artifacts.example.com/npm/",
        "BOSS_NPM_CREDENTIAL_REF": "secret_ref:sref_npm",
        "BOSS_NPM_TOKEN": "npm_secret_do_not_expose",
        "BOSS_PIP_INDEX_URL": "https://artifacts.example.com/pypi/simple",
        "BOSS_PIP_CREDENTIAL_REF": "store:boss/registry/pypi",
        "BOSS_PIP_PASSWORD": "pip_secret_do_not_expose",
        "BOSS_MAVEN_MIRROR": "https://artifacts.example.com/maven/all",
        "BOSS_MAVEN_CREDENTIAL_REF": "env",
        "BOSS_MAVEN_TOKEN": "maven_secret_do_not_expose",
    }
    env = agent_environment(source)
    assert env["npm_config_registry"] == "http://egress:8090/npm/"
    assert env["YARN_NPM_REGISTRY_SERVER"] == "http://egress:8090/npm/"
    assert env["PIP_INDEX_URL"] == "http://egress:8090/pypi/"
    dumped = json.dumps(env)
    assert "npm_secret_do_not_expose" not in dumped
    assert "pip_secret_do_not_expose" not in dumped
    assert "BOSS_NPM_CREDENTIAL_REF" not in env
    assert "BOSS_PIP_CREDENTIAL_REF" not in env

    for k, v in source.items():
        monkeypatch.setenv(k, v)
    home = tmp_path / "home"
    home.mkdir()
    assert toolchains.write_proxy_config(home, "http://egress:8091")
    settings_xml = (home / ".m2" / "settings.xml").read_text()
    assert "<url>http://egress:8090/maven/</url>" in settings_xml
    assert "maven_secret_do_not_expose" not in settings_xml
    gradle_init = (home / ".gradle" / "init.d" / "boss-mirror.gradle").read_text()
    assert "http://egress:8090/maven/" in gradle_init
    assert "allowInsecureProtocol = true" in gradle_init
    assert en.problems(source) == []
