"""A Java app (Maven or Gradle) previews in a container, deploys, and is pre-checked like any app.

BOSS already gated, built and host-previewed Java apps (toolchains, stacks, preview_service.
command_for_entry), but the last mile was Node/Python-only:

  * the container preview's Dockerfile generator had no Java branch: a resolved `java` entry fell
    through to "could not infer a stack", and a Java app's own declared command got
    python:3.11-slim as its base image;
  * the one-container deploy had no Java mode: a Java app fell through to static and failed
    "no index.html";
  * the pre-check checked node/npm/cc for every app, found no Spring route or entry, and so
    reported a Java app's toolchain and health path wrongly;
  * an agent was told the Node facts of its sandbox and nothing about Java.

The tests are local (no Docker, no network) except the last, which builds a real Spring Boot
template jar when this machine has Maven and can reach Maven Central.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from boss_engine.services import deploy_service, preview_service, toolchains
from boss_engine.services import workflow_precheck as wp

ROOT = Path(__file__).resolve().parents[2]
SPRING_TEMPLATE = ROOT / "templates" / "apps" / "login-oauth" / "stacks" / "java"


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


POM = "<project><artifactId>demo</artifactId></project>\n"


# ── the shared helpers: one place knows how a Java app builds and runs ─────────────────────────

def test_the_preview_command_is_unchanged_by_the_refactor(tmp_path):
    """java_preview_command was split into helpers the deploy reuses; its output is the same."""
    maven = toolchains.java_preview_command(tmp_path, "maven")
    assert maven[:2] == ["sh", "-lc"]
    assert maven[2] == (
        'mvn -B -ntp -q -DskipTests package && jar=$(ls target/*.jar 2>/dev/null | grep -v -e '
        '"original-" -e "-plain.jar" -e "-sources.jar" -e "-javadoc.jar" | head -n 1); '
        '[ -n "$jar" ] || { echo "no jar under target" >&2; exit 1; }; '
        'exec java -Dserver.port="$PORT" -jar "$jar"')
    (tmp_path / "config.yml").write_text("server: {}\n")
    gradle = toolchains.java_preview_command(tmp_path, "gradle")[2]
    assert gradle.startswith("gradle --no-daemon -q -x test assemble && ")
    assert "build/libs/*.jar" in gradle and gradle.endswith('-jar "$jar" server config.yml')


@pytest.mark.parametrize("build,image", [("maven", "maven:3.9-eclipse-temurin-21"),
                                         ("gradle", "gradle:8.14-jdk21")])
def test_a_java_dockerfile_builds_with_its_tool_and_runs_on_a_jre(build, image):
    text = toolchains.java_dockerfile(build)
    stages = [line for line in text.splitlines() if line.startswith("FROM ")]
    assert stages == [f"FROM {image} AS build", f"FROM {toolchains.JAVA_RUNTIME_IMAGE}"]
    assert toolchains.java_package_command(build) in text
    assert "COPY --from=build /tmp/app.jar /app/app.jar" in text
    assert "EXPOSE 8080" in text and "PORT=8080" in text
    assert 'exec java -Dserver.port=$PORT -jar /app/app.jar"]' in text


def test_a_java_dockerfile_for_an_app_in_a_folder_with_a_dropwizard_config():
    text = toolchains.java_dockerfile("maven", "api/", config="config.yml")
    assert "WORKDIR /src/api\n" in text
    assert "COPY --from=build /src/api/config.yml /app/config.yml" in text
    assert "-jar /app/app.jar server config.yml" in text


def test_a_java_dockerfile_refuses_what_is_not_a_java_build():
    with pytest.raises(ValueError):
        toolchains.java_dockerfile("npm")


# ── the container preview ─────────────────────────────────────────────────────────────────────

def test_a_java_app_gets_a_java_image_not_could_not_infer_a_stack(tmp_path):
    _write(tmp_path, {"pom.xml": POM, "src/main/java/App.java": "class App {}\n"})
    port = preview_service._generate_dockerfile(tmp_path)  # noqa: SLF001
    text = (tmp_path / "Dockerfile").read_text()
    assert port == toolchains.JAVA_PORT
    assert text.startswith("FROM maven:3.9-eclipse-temurin-21 AS build")


def test_a_java_apps_own_run_command_gets_a_jdk_not_python(tmp_path):
    _write(tmp_path, {"build.gradle": "plugins { id 'java' }\n",
                      "boss.preview.command": "gradle bootRun\n"})
    port = preview_service._generate_dockerfile(tmp_path)  # noqa: SLF001
    text = (tmp_path / "Dockerfile").read_text()
    assert port == toolchains.JAVA_PORT
    assert text.startswith("FROM gradle:8.14-jdk21\n") and "python" not in text
    assert "gradle bootRun" in text


def test_a_node_app_beside_a_pom_is_still_a_node_app(tmp_path):
    _write(tmp_path, {"pom.xml": POM, "boss.preview.command": "npm start\n",
                      "package.json": '{"scripts": {"start": "node s.js"}}'})
    port = preview_service._generate_dockerfile(tmp_path)  # noqa: SLF001
    assert port == 3000
    assert (tmp_path / "Dockerfile").read_text().startswith("FROM node:20-slim")


def test_a_python_declaration_still_gets_python(tmp_path):
    _write(tmp_path, {"requirements.txt": "flask\n", "boss.preview.command": "python app.py\n"})
    preview_service._generate_dockerfile(tmp_path)  # noqa: SLF001
    assert (tmp_path / "Dockerfile").read_text().startswith("FROM python:3.11-slim")


# ── the one-container deploy ──────────────────────────────────────────────────────────────────

def test_a_java_deploy_runs_the_jar_on_a_jre_with_data_kept(monkeypatch):
    cmd = deploy_service._docker_run_cmd("demo", 8101, "/srv/boss/demo", "java")  # noqa: SLF001
    assert toolchains.JAVA_RUNTIME_IMAGE in cmd
    assert "/srv/boss/demo/dist:/app:ro" in cmd and "/srv/boss/demo/data:/data" in cmd
    assert "-e PORT=80" in cmd and "-p 8101:80" in cmd
    assert "-jar /app/app.jar" in cmd and "server " not in cmd
    dw = deploy_service._docker_run_cmd("demo", 8101, "/srv/boss/demo", "java",  # noqa: SLF001
                                        "config.yml")
    assert "-jar /app/app.jar server config.yml" in dw


def test_the_other_deploy_modes_are_untouched():
    for mode, marker in (("node", "node:22-bookworm-slim"), ("py", "python:3.12-slim"),
                         ("static", "nginx:alpine")):
        cmd = deploy_service._docker_run_cmd("demo", 8101, "/srv/x", mode, "python3 app.py")  # noqa: SLF001
        assert marker in cmd and toolchains.JAVA_RUNTIME_IMAGE not in cmd, mode


def _fake_build(monkeypatch, *, ok: bool = True) -> list[dict[str, Any]]:
    """owned_process.run as a package step: writes the jar where BOSS_JAR_OUT says (or fails)."""
    calls: list[dict[str, Any]] = []

    def run(argv, **kw):
        calls.append({"argv": argv, **kw})
        if ok:
            Path(kw["env"]["BOSS_JAR_OUT"]).write_bytes(b"PK\x03\x04jar")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "[ERROR] COMPILATION ERROR : App.java")

    monkeypatch.setattr(deploy_service.owned_process, "run", run)
    return calls


def test_a_java_deploy_packages_the_jar_and_its_dropwizard_config(tmp_path, monkeypatch):
    work = _write(tmp_path / "work", {"api/pom.xml": POM, "api/config.yml": "server: {}\n"})
    monkeypatch.setattr(toolchains, "has", lambda name: name == "java")
    calls = _fake_build(monkeypatch)
    payload, mode, entry, sha = deploy_service._java_payload(work, "api", "abc123")  # noqa: SLF001
    try:
        assert mode == "java" and entry == "config.yml" and sha == "abc123"
        assert (payload / "dist" / "app.jar").read_bytes().startswith(b"PK")
        assert (payload / "dist" / "config.yml").is_file()
        assert calls[0]["cwd"] == str(work / "api")
        assert toolchains.java_package_command("maven") in calls[0]["argv"][2]
        assert not work.exists()                       # the checkout is cleaned up
    finally:
        shutil.rmtree(payload, ignore_errors=True)


def test_a_java_deploy_without_the_toolchain_says_how_to_turn_it_on(tmp_path, monkeypatch):
    work = _write(tmp_path / "work", {"pom.xml": POM})
    monkeypatch.setattr(toolchains, "has", lambda name: False)
    with pytest.raises(RuntimeError, match="boss toolchains enable java"):
        deploy_service._java_payload(work, ".", None)  # noqa: SLF001


def test_a_java_build_that_fails_says_why(tmp_path, monkeypatch):
    work = _write(tmp_path / "work", {"build.gradle": "plugins { id 'java' }\n"})
    monkeypatch.setattr(toolchains, "has", lambda name: name == "java")
    _fake_build(monkeypatch, ok=False)
    with pytest.raises(RuntimeError, match="COMPILATION ERROR"):
        deploy_service._java_payload(work, ".", None)  # noqa: SLF001


def test_a_java_deploy_waits_longer_for_health():
    import inspect

    src = inspect.getsource(deploy_service)
    assert deploy_service.JAVA_DEPLOY_HEALTH_TIMEOUT_SECONDS > \
        deploy_service.DEPLOY_HEALTH_TIMEOUT_SECONDS
    assert ('timeout=(JAVA_DEPLOY_HEALTH_TIMEOUT_SECONDS if mode == "java"' in src)


# ── the pre-check ─────────────────────────────────────────────────────────────────────────────

class _Settings:
    boss_edition = "community"


def _ctx(app_dir: Path) -> wp.Context:
    ctx = wp.Context(connection=None, settings=_Settings(), project_id="p1", actor="ceo",
                     spec={"steps": []}, run_input={}, checkpoint=wp.PRE_DEPLOY,
                     app_dir=app_dir, offline=True)
    return ctx


SPRING = '''
@RestController
@RequestMapping("/api")
public class Health {
  @GetMapping("/healthz") public String ok() { return "ok"; }
  @RequestMapping(value = "/tickets", method = RequestMethod.GET) public List<T> all() {}
  @PostMapping(path = {"/login"}) public void login() {}
}
'''
MAIN = "public static void main(String[] a) { SpringApplication.run(App.class, a); }\n"
JAXRS = '@Path("/status")\npublic class Status extends Application<Config> {}\n'


def test_the_pre_check_reads_spring_and_jaxrs_routes(tmp_path):
    _write(tmp_path, {"pom.xml": POM, "src/main/java/Health.java": SPRING,
                      "src/main/java/Status.java": JAXRS,
                      "target/generated-sources/Gen.java": '@GetMapping("/generated")\n'})
    routes = wp._routes(_ctx(tmp_path))  # noqa: SLF001
    assert {"/api", "/healthz", "/tickets", "/login", "/status"} <= routes
    assert "/generated" not in routes            # Maven's target/ is output, not the app


def test_spring_actuator_serves_its_health_without_a_route(tmp_path):
    _write(tmp_path, {"pom.xml": "<artifactId>spring-boot-starter-actuator</artifactId>\n"})
    assert "/actuator/health" in wp._routes(_ctx(tmp_path))  # noqa: SLF001


def test_the_pre_check_finds_a_spring_or_dropwizard_entry(tmp_path):
    _write(tmp_path, {"pom.xml": POM, "src/main/java/App.java": MAIN,
                      "src/main/java/Status.java": JAXRS})
    found = wp._server_entry(_ctx(tmp_path))  # noqa: SLF001
    assert sorted(found) == ["src/main/java/App.java", "src/main/java/Status.java"]


def test_a_java_app_without_the_java_toolchain_will_fail_before_the_run(tmp_path, monkeypatch):
    _write(tmp_path, {"pom.xml": POM})
    monkeypatch.setattr(toolchains, "installed", lambda: {})
    out = wp.check_toolchain(_ctx(tmp_path), {"id": "build"})
    assert out["verdict"] == "will_fail"
    assert "boss toolchains enable java" in out["fix"]["action"]
    assert "node" not in out["evidence"]


def test_a_java_app_with_the_toolchain_is_ready(tmp_path, monkeypatch):
    _write(tmp_path, {"build.gradle": "plugins { id 'java' }\n"})
    monkeypatch.setattr(toolchains, "installed",
                        lambda: {"java": {"jdk": "21.0.5", "gradle": "8.14"}})
    monkeypatch.setattr(wp.shutil, "which", lambda t: f"/usr/bin/{t}")
    out = wp.check_toolchain(_ctx(tmp_path), {"id": "build"})
    assert out["verdict"] == "ready" and "gradle build" in out["evidence"]


def test_a_node_app_beside_a_pom_is_checked_as_node(tmp_path, monkeypatch):
    _write(tmp_path, {"pom.xml": POM, "package.json": "{}"})
    monkeypatch.setattr(toolchains, "installed", lambda: {})
    out = wp.check_toolchain(_ctx(tmp_path), {"id": "build"})
    assert "node" in out["evidence"] and "maven build" not in out["evidence"]


# ── what an agent is told ─────────────────────────────────────────────────────────────────────

def test_an_agent_is_told_the_java_facts_only_when_java_is_installed(monkeypatch):
    from boss_engine.services import code_agent

    monkeypatch.setattr(toolchains, "installed", lambda: {})
    assert code_agent.java_fact() == ""
    monkeypatch.setattr(toolchains, "installed",
                        lambda: {"java": {"jdk": "21.0.5", "maven": "3.9.9", "gradle": "8.14"}})
    fact = code_agent.java_fact()
    assert "JDK 21.0.5, Maven 3.9.9, Gradle 8.14" in fact
    assert "never ./gradlew" in fact and "$PORT" in fact


# ── a real Spring Boot build (this machine's Maven, Maven Central) ────────────────────────────

def _maven_central_reachable() -> bool:
    if not shutil.which("mvn") or not shutil.which("java"):
        return False
    try:
        out = subprocess.run(["curl", "-sS", "-m", "8", "-o", "/dev/null", "-w", "%{http_code}",
                              "https://repo.maven.apache.org/maven2/"],
                             capture_output=True, text=True, timeout=12)
        return out.stdout.strip() == "200"
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(os.environ.get("BOSS_REAL_JAVA_BUILD") != "1" or not _maven_central_reachable(),
                    reason="set BOSS_REAL_JAVA_BUILD=1 on a machine with Maven and Maven Central")
def test_a_real_spring_boot_template_packages_into_a_runnable_jar(tmp_path, monkeypatch):
    work = tmp_path / "work"
    shutil.copytree(SPRING_TEMPLATE, work / "app")
    monkeypatch.setattr(toolchains, "has", lambda name: name == "java")
    payload, mode, entry, _ = deploy_service._java_payload(work, "app", None)  # noqa: SLF001
    try:
        jar = payload / "dist" / "app.jar"
        assert mode == "java" and entry == ""
        with zipfile.ZipFile(jar) as z:                 # a Spring Boot fat jar, not a plain one
            names = z.namelist()
        assert any(n.startswith("BOOT-INF/classes/") for n in names)
        assert "META-INF/MANIFEST.MF" in names
    finally:
        shutil.rmtree(payload, ignore_errors=True)
