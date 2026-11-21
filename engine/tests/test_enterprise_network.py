"""The enterprise network block is ONE list, and every consumer agrees with it.

An install inside a company network needs registry mirrors, an outbound proxy, the company's CA
and additions to the sandbox fence (docs/community/ENTERPRISE_NETWORK.md). Before this, the one
setting that existed (BOSS_NPM_REGISTRY) was validated at run time but not at build time, so a URL
with a credential in it was baked into `docker history`; an upgrade that exported a new value
silently kept the old one; and nothing else (pip, Go, Maven, the proxy, the CA) had a setting.

The guards here are classes, not instances:

1. Every URL setting refuses a credential, a query, a fragment and plain http — and never echoes
   the value it refused.
2. Every setting in enterprise_network.SETTINGS is written by the installer, carried by the
   upgrade with "a new value wins", documented in .env.example, and reaches every BOSS container
   built from the api image; every image build gets the build settings and the CA secret.
3. A run gets the mirrors, with nothing inherited left to contradict them, and fails closed on a
   bad value.
"""
from __future__ import annotations

import base64
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from boss_engine.services import enterprise_network as en
from boss_engine.services.agent_env import agent_environment
from boss_engine.services.agent_shell import _SHELL_ENV_KEYS

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "scripts" / "install" / "community-compose.yml"
SECRET = "s3cr3t-value-never-printed"
#: Every BOSS service built from the api image (python-runtime): each must see the block.
PYTHON_RUNTIME_SERVICES = ("api", "worker", "runner", "egress", "model-gateway", "mcp-tools", "kg-mcp")


@pytest.mark.parametrize("name", sorted(en.URL_SETTINGS))
@pytest.mark.parametrize("value", [
    f"https://user:{SECRET}@registry.example.com/npm/",
    f"https://{SECRET}@registry.example.com/npm/",
    f"https://registry.example.com/npm/?token={SECRET}",
    f"https://registry.example.com/npm/#{SECRET}",
    f"http://registry.example.com/{SECRET}/",
    f"https://registry.example.com/{SECRET}\nOTHER=1",
    "https:///no-host",
])
def test_every_url_setting_refuses_a_credential_and_never_echoes_it(name, value):
    found = en.problems({name: value})
    assert found and name in found[0]
    assert SECRET not in " ".join(found)


def test_a_plain_mirror_url_is_accepted_and_a_proxy_may_be_http():
    env = {name: "https://artifacts.example.com:8443/api/x/" for name in en.URL_SETTINGS}
    env["BOSS_UPSTREAM_PROXY"] = "http://proxy.example.com:8080"
    assert en.problems(env) == []
    assert en.problems({"BOSS_UPSTREAM_PROXY": f"http://me:{SECRET}@proxy.example.com:8080"})
    assert SECRET not in " ".join(en.problems({"BOSS_UPSTREAM_PROXY": f"http://me:{SECRET}@p.example.com"}))


@pytest.mark.parametrize("name,value,ok", [
    ("BOSS_EGRESS_CONNECT_ALLOW", "artifacts.example.com,*.example.org,nexus.example.com:8443", True),
    ("BOSS_EGRESS_CONNECT_ALLOW", "nexus.example.com:99999", False),
    ("BOSS_EGRESS_CONNECT_ALLOW", "10.0.0.5", False),
    ("BOSS_EGRESS_PRIVATE_ALLOW", "nexus.corp.example.com:8081", True),
    ("BOSS_EGRESS_PRIVATE_ALLOW", "*.corp.example.com", False),       # exact names only
    ("BOSS_EGRESS_PRIVATE_ALLOW", "db", False),                       # never a BOSS service name
    ("BOSS_GOSUMDB", "off", False),                                   # checking is never off
    ("BOSS_GOSUMDB", "sum.golang.org https://artifacts.example.com/gosumdb", True),
    ("BOSS_GONOSUMDB", "git.example.com/*,example.com/internal", True),
    ("BOSS_EGRESS_CONNECT_DEFAULTS", "maybe", False),
])
def test_list_and_word_settings(name, value, ok):
    assert (en.problems({name: value}) == []) is ok


def _ca(tmp_path: Path) -> Path:
    if not shutil.which("openssl"):
        pytest.skip("openssl is needed to make a test CA")
    key, cert = tmp_path / "ca.key", tmp_path / "ca.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "1", "-subj", "/CN=Example Inspection Root"],
                   check=True, capture_output=True, timeout=60)
    return cert


def test_the_ca_file_must_be_certificates_and_never_a_key(tmp_path):
    cert = _ca(tmp_path)
    assert en.problems({"BOSS_EXTRA_CA_FILE": str(cert)}) == []
    bundle = tmp_path / "with-key.pem"
    bundle.write_bytes(cert.read_bytes() + (tmp_path / "ca.key").read_bytes())
    assert "PRIVATE KEY" in en.problems({"BOSS_EXTRA_CA_FILE": str(bundle)})[0]
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate\n")
    assert en.problems({"BOSS_EXTRA_CA_FILE": str(junk)})
    assert en.problems({"BOSS_EXTRA_CA_FILE": "relative/ca.pem"})
    assert en.problems({"BOSS_EXTRA_CA_FILE": str(tmp_path / "missing.pem")})


def test_finalize_derives_the_containers_copy_and_takes_over_the_earlier_name(tmp_path):
    cert = _ca(tmp_path)
    env = tmp_path / ".env"
    env.write_text(f"A=1\nBOSS_EXTRA_CA_FILE=\nBOSS_EXTRA_CA_PEM_B64=stale\nBOSS_BUILD_CA_FILE={cert}\n")
    assert en.finalize(str(env)) == []
    values = en.read_env_file(str(env))
    assert values["BOSS_EXTRA_CA_FILE"] == str(cert)
    assert base64.b64decode(values["BOSS_EXTRA_CA_PEM_B64"]) == cert.read_bytes()
    assert values["A"] == "1"
    # Cleared in .env: the containers' copy goes too (a removed CA must stop being trusted).
    env.write_text("BOSS_EXTRA_CA_FILE=\nBOSS_EXTRA_CA_PEM_B64=old\n")
    assert en.finalize(str(env)) == []
    assert en.read_env_file(str(env))["BOSS_EXTRA_CA_PEM_B64"] == ""


def test_finalize_refuses_without_echoing_and_the_cli_exits_non_zero(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"BOSS_PIP_INDEX_URL=https://me:{SECRET}@pypi.example.com/simple\n")
    result = subprocess.run(["python3", str(REPO / "engine/boss_engine/services/enterprise_network.py"),
                             "finalize", str(env)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "BOSS_PIP_INDEX_URL" in result.stderr and SECRET not in result.stderr + result.stdout


# ---- what a run gets ---------------------------------------------------------------------------

def test_a_run_gets_every_mirror_and_nothing_inherited_contradicts_it():
    source = {"BOSS_NPM_REGISTRY": "https://npm.example.com/r/", "NPM_CONFIG_REGISTRY": "https://old.example.com/",
              "NPM_CONFIG_STRICT_SSL": "false", "BOSS_PIP_INDEX_URL": "https://pypi.example.com/simple",
              "PIP_EXTRA_INDEX_URL": "https://pypi.org/simple", "PIP_TRUSTED_HOST": "pypi.example.com",
              "BOSS_PLAYWRIGHT_DOWNLOAD_HOST": "https://pw.example.com/",
              "HTTPS_PROXY": "http://proxy.example.com:8080", "NO_PROXY": ".example.com",
              "GIT_SSL_CAINFO": "/var/lib/boss/trust/ca-bundle.pem", "DATABASE_URL": "private"}
    env = agent_environment(source)
    assert env["npm_config_registry"] == "https://npm.example.com/r/"
    assert env["npm_config_strict_ssl"] == "true"
    assert env["PIP_INDEX_URL"] == "https://pypi.example.com/simple"
    assert env["PLAYWRIGHT_DOWNLOAD_HOST"] == "https://pw.example.com"
    for gone in ("NPM_CONFIG_REGISTRY", "NPM_CONFIG_STRICT_SSL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST",
                 "DATABASE_URL", "BOSS_NPM_REGISTRY"):
        assert gone not in env, gone
    assert env["GIT_SSL_CAINFO"] and env["HTTPS_PROXY"] and env["NO_PROXY"]


def test_a_bad_mirror_fails_closed_rather_than_falling_back_to_the_public_registry():
    with pytest.raises(ValueError):
        agent_environment({"BOSS_PIP_INDEX_URL": "http://pypi.example.com/simple"})


def test_the_console_agent_shell_gets_the_same_trust_mirrors_and_proxy():
    for key in en.PACKAGE_ENV_KEYS + en.CA_ENV_KEYS + en.PROXY_ENV_KEYS:
        assert key in _SHELL_ENV_KEYS, key


def test_go_runs_use_the_company_module_proxy_and_its_internal_paths(monkeypatch):
    from boss_engine.services import toolchains

    monkeypatch.setattr(toolchains, "has", lambda name: name == "go")
    monkeypatch.setenv("BOSS_GOPROXY", "https://artifacts.example.com/api/go/go-virtual")
    monkeypatch.setenv("BOSS_GOSUMDB", "sum.golang.org https://artifacts.example.com/api/go/gosumdb")
    monkeypatch.setenv("BOSS_GONOSUMDB", "git.example.com/*")
    env = toolchains.sandbox_env("/home/run")
    assert env["GOPROXY"] == "https://artifacts.example.com/api/go/go-virtual"
    assert env["GOSUMDB"].endswith("/gosumdb")
    assert env["GONOSUMDB"] == "git.example.com/*"
    assert env["GOPRIVATE"] == "" and env["GOINSECURE"] == ""          # still nothing skips the proxy
    for name in ("BOSS_GOPROXY", "BOSS_GOSUMDB", "BOSS_GONOSUMDB"):
        monkeypatch.delenv(name)
    env = toolchains.sandbox_env("/home/run")
    assert env["GOPROXY"] == toolchains.GOPROXY and env["GONOSUMDB"] == ""


def test_maven_runs_use_the_company_mirror(monkeypatch, tmp_path):
    from boss_engine.services import toolchains

    monkeypatch.setattr(toolchains, "has", lambda name: name == "java")
    monkeypatch.setenv("BOSS_MAVEN_MIRROR", "https://artifacts.example.com/api/maven/all")
    assert toolchains.write_proxy_config(tmp_path, "http://egress:8091")
    settings = (tmp_path / ".m2/settings.xml").read_text()
    assert "<mirrorOf>*</mirrorOf><url>https://artifacts.example.com/api/maven/all</url>" in settings
    monkeypatch.delenv("BOSS_MAVEN_MIRROR")
    assert toolchains.write_proxy_config(tmp_path, "http://egress:8091")
    assert "<mirrors>" not in (tmp_path / ".m2/settings.xml").read_text()


def test_java_builds_trust_the_extra_ca_through_their_own_config(monkeypatch, tmp_path):
    from boss_engine.services import toolchains

    store = tmp_path / "cacerts"
    store.write_bytes(b"jks")
    monkeypatch.setattr(toolchains, "has", lambda name: name == "java")
    monkeypatch.setenv(toolchains.JAVA_TRUST_STORE_ENV, str(store))
    home = tmp_path / "home"
    home.mkdir()
    assert toolchains.write_proxy_config(home, "http://egress:8091")
    assert f"javax.net.ssl.trustStore={store}" in (home / ".gradle/gradle.properties").read_text()
    assert f"-Djavax.net.ssl.trustStore={store}" in (home / ".mavenrc").read_text()


def test_registry_hosts_and_ports_are_what_the_fence_allows():
    env = {"BOSS_NPM_REGISTRY": "https://npm.example.com/r/", "BOSS_PIP_INDEX_URL": "https://nexus.example.com:8443/simple",
           "BOSS_GOSUMDB": "sum.golang.org https://sumdb.example.com/", "BOSS_MAVEN_MIRROR": f"https://x:{SECRET}@bad.example.com/"}
    assert en.registry_endpoints(env) == [("nexus.example.com", 8443), ("npm.example.com", 443),
                                          ("sumdb.example.com", 443)]


# ---- one list, every consumer --------------------------------------------------------------------

def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def test_every_boss_container_built_from_the_api_image_sees_the_whole_block():
    services = _compose()["services"]
    runtime = set(en.SETTINGS) - {"BOSS_EXTRA_CA_FILE"}      # the host path; containers get the copy
    for name in PYTHON_RUNTIME_SERVICES:
        missing = runtime - set(services[name]["environment"])
        assert not missing, f"{name} does not get {sorted(missing)}"


def test_every_image_build_gets_the_build_settings_and_the_ca_secret_never_a_credential():
    compose = _compose()
    build_settings = {"BOSS_NPM_REGISTRY", "BOSS_PIP_INDEX_URL", "BOSS_PLAYWRIGHT_DOWNLOAD_HOST",
                      "BOSS_UPSTREAM_PROXY", "BOSS_UPSTREAM_NO_PROXY"}
    targets = {n: s["build"] for n, s in compose["services"].items()
               if isinstance(s.get("build"), dict) and s["build"].get("dockerfile") == "Dockerfile.community"}
    assert len(targets) >= 7
    for name, build in targets.items():
        assert "boss_build_ca" in build.get("secrets", []), name
        assert build_settings <= set(build["args"]), name
        for arg in build["args"]:
            assert not re.search(r"TOKEN|PASSWORD|SECRET|_KEY|CA_PEM|CA_FILE", arg), (name, arg)
    assert "BOSS_EXTRA_CA_FILE" in compose["secrets"]["boss_build_ca"]["file"]


def test_the_installer_writes_and_the_upgrade_carries_every_setting_with_a_new_value_winning():
    install = (REPO / "install.sh").read_text()
    heredoc = install.split('cat > "$_new_env" <<EOF', 1)[1].split("\nEOF\n", 1)[0]
    preserve = (REPO / "scripts/install/restore_preserved_env.sh").read_text()
    fill_if_blank = preserve.split('FILL_IF_BLANK="', 1)[1].split('"', 1)[0].split()
    keep_previous = preserve.split('KEEP_PREVIOUS="', 1)[1].split('"', 1)[0].split()
    example = (REPO / ".env.example").read_text()
    for name in en.SETTINGS:
        assert re.search(rf"^{name}=", heredoc, re.M), f"install.sh does not write {name}"
        assert re.search(rf"^{name}=", example, re.M), f".env.example does not document {name}"
        assert name not in keep_previous, f"{name}: an exported new value must replace the saved one"
        if name != "BOSS_EXTRA_CA_PEM_B64":            # derived by `finalize`, never carried
            assert name in fill_if_blank, f"{name} is lost on upgrade"
    # The settings are checked before the new .env replaces the live one.
    assert install.index("enterprise_network.py\" finalize") < install.index('mv "$_new_env" "$ENV_FILE"')


def test_the_runtime_wrapper_keeps_boss_services_off_the_proxy():
    wrapper = (REPO / "scripts/install/with-extra-ca.sh").read_text()
    listed = wrapper.split('_wec_np="', 1)[1].split('"', 1)[0].split(",")
    assert listed == list(en.BOSS_NO_PROXY)
    services = set(_compose()["services"])
    assert services - {"db-test"} <= set(en.BOSS_NO_PROXY) | {"infisical-redis"}


def test_no_proxy_matching():
    env = {"BOSS_UPSTREAM_NO_PROXY": ".corp.example.com,10.0.0.0/8,git.example.org"}
    assert en.bypasses_proxy("api", env) and en.bypasses_proxy("egress", env)
    assert en.bypasses_proxy("nexus.corp.example.com", env) and en.bypasses_proxy("10.1.2.3", env)
    assert en.bypasses_proxy("git.example.org", env)
    assert not en.bypasses_proxy("registry.npmjs.org", env)
