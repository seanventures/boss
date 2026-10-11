"""PER-STACK TOOLCHAINS IN THE BUILD SANDBOX (docs/community/TOOLCHAINS.md).

The API/worker image always carries Python and Node. Anything else is opt-in per install:
`BOSS_TOOLCHAINS=java` in .env selects the `toolchains-java` stage of Dockerfile.community, which
puts a pinned, checksum-verified Temurin JDK, Maven and Gradle under /opt/toolchains and writes
/opt/toolchains/manifest.json. An install without it carries an empty manifest and nothing else.

This module is the one place that knows:

* which toolchains this image has (`installed`, read from the manifest);
* which stack a checkout is (`detect_java`: pom.xml, build.gradle, build.gradle.kts);
* the commands that test and build it (`java_gate_commands`) and run it (`java_preview_command`);
* what to say when the toolchain is not there (`missing_message`) — in words, never "mvn: not
  found";
* how a sandboxed run reaches Maven Central and the Gradle plugin portal (only through
  boss-egress's CONNECT proxy: `write_proxy_config`), and how its dependency caches persist per
  PROJECT across runs without ever being handed to another project (`restore_cache`,
  `save_cache`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

log = logging.getLogger("boss.toolchains")

MANIFEST_ENV = "BOSS_TOOLCHAINS_MANIFEST"
DEFAULT_MANIFEST = "/opt/toolchains/manifest.json"
CACHE_ROOT_ENV = "BOSS_TOOLCHAIN_CACHE_ROOT"
DEFAULT_CACHE_ROOT = "/var/lib/boss/toolchain-cache"
CACHE_MAX_ENV = "BOSS_TOOLCHAIN_CACHE_MAX_MB"
DEFAULT_CACHE_MAX_MB = 2048

#: Every toolchain an install can turn on, and how to say so.
KNOWN = {"java": "Java (Temurin JDK 21, Maven, Gradle)",
         # The security review's scanners (APP_TEMPLATES_AND_SECURITY_LOOP.md §4.5); the stage is
         # built on the Java one because ZAP needs a JRE, so this install has Java too.
         "security": "Security scanners (Semgrep, OSV-Scanner, OWASP ZAP; includes Java)"}
ENABLE_HINT = "boss toolchains enable {name}"

MAVEN_FILES = ("pom.xml",)
GRADLE_FILES = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")

#: Batch mode, no transfer progress: a log a person (and the repair agent) can read.
MAVEN_GATE = ["mvn -B -ntp verify"]
#: The image's Gradle, not ./gradlew: the wrapper downloads a distribution from
#: services.gradle.org, which the sandbox's egress allowlist does not include.
GRADLE_GATE = ["gradle --no-daemon --console=plain build"]
GATE_TIMEOUT_SECONDS = 1200

#: Per-project caches, relative to the sandbox HOME: (cache name, path under HOME).
#: Gradle's downloaded modules only — its transforms and daemons are per-machine scratch.
CACHE_DIRS = (("m2", ".m2/repository"), ("gradle", ".gradle/caches/modules-2"))


def manifest_path() -> Path:
    return Path(os.environ.get(MANIFEST_ENV) or DEFAULT_MANIFEST)


def installed() -> dict[str, dict[str, str]]:
    """{"java": {"jdk": "...", "maven": "...", "gradle": "..."}} — what this image carries."""
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): {str(a): str(b) for a, b in v.items()} for k, v in data.items()
            if isinstance(v, dict)}


def has(name: str) -> bool:
    return name in installed()


def setup_summary() -> list[dict[str, Any]]:
    """For Setup: every known toolchain, on or off, with its versions and how to turn it on."""
    have = installed()
    out = [{"name": "python", "label": "Python", "installed": True, "builtin": True,
            "versions": {}},
           {"name": "node", "label": "Node", "installed": True, "builtin": True, "versions": {}}]
    for name, label in KNOWN.items():
        out.append({"name": name, "label": label, "installed": name in have, "builtin": False,
                    "versions": have.get(name, {}),
                    "enable": None if name in have else ENABLE_HINT.format(name=name)})
    return out


# ---- stack detection ---------------------------------------------------------------------------

def detect_java(root: Path | str) -> Optional[str]:
    """"maven" or "gradle" when the checkout's root is a Java build, else None. A pom.xml wins
    over Gradle files (a repo carrying both builds with Maven)."""
    base = Path(root)
    if any((base / name).is_file() for name in MAVEN_FILES):
        return "maven"
    if any((base / name).is_file() for name in GRADLE_FILES):
        return "gradle"
    return None


def java_gate_commands(build: str) -> list[str]:
    return list(MAVEN_GATE if build == "maven" else GRADLE_GATE)


def build_file(build: str) -> str:
    return "pom.xml" if build == "maven" else "build.gradle"


def missing_message(name: str, found: str) -> str:
    """The words a build shows when the project needs a toolchain this install does not have."""
    label = {"java": "Java", "security": "the security scanners"}.get(name, name)
    return (f"This project needs {label} (it has a {found}), and {label} is not turned on for "
            f"this BOSS. Turn it on with `{ENABLE_HINT.format(name=name)}` on the BOSS host "
            f"(or set BOSS_TOOLCHAINS={name} in .env and run `boss update`), then run the build "
            f"again.")


def java_preview_command(app_root: Path, build: str) -> list[str]:
    """Build the jar, then run it on $PORT. A Dropwizard app (a config.yml beside the build)
    runs as `server config.yml`; its config reads the port from ${PORT} (see TOOLCHAINS.md).
    Spring Boot takes -Dserver.port. Anything else declares boss.preview.command."""
    if build == "maven":
        package = "mvn -B -ntp -q -DskipTests package"
        jars = "target/*.jar"
    else:
        package = "gradle --no-daemon -q -x test assemble"
        jars = "build/libs/*.jar"
    config = next((n for n in ("config.yml", "config.yaml") if (app_root / n).is_file()), None)
    args = f" server {config}" if config else ""
    pick = (f'jar=$(ls {jars} 2>/dev/null | grep -v -e "original-" -e "-plain.jar" '
            f'-e "-sources.jar" -e "-javadoc.jar" | head -n 1); '
            f'[ -n "$jar" ] || {{ echo "no jar under {jars.rsplit("/", 1)[0]}" >&2; exit 1; }}; ')
    return ["sh", "-lc",
            f'{package} && {pick}exec java -Dserver.port="$PORT" -jar "$jar"{args}']


# ---- the sandbox: proxy config and per-project caches -----------------------------------------

def _proxy_host_port(proxy_url: str) -> Optional[tuple[str, int]]:
    try:
        parts = urlsplit(proxy_url)
        if parts.hostname and parts.port:
            return parts.hostname, parts.port
    except ValueError:
        pass
    return None


NON_PROXY = "localhost|127.0.0.1|[::1]|api|egress"


def maven_settings_xml(host: str, port: int) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- Written by BOSS for this sandboxed run: every download goes through boss-egress. -->\n"
        '<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">\n'
        "  <proxies>\n"
        f"    <proxy><id>boss-egress</id><active>true</active><protocol>https</protocol>"
        f"<host>{host}</host><port>{port}</port><nonProxyHosts>{NON_PROXY}</nonProxyHosts></proxy>\n"
        f"    <proxy><id>boss-egress-http</id><active>true</active><protocol>http</protocol>"
        f"<host>{host}</host><port>{port}</port><nonProxyHosts>{NON_PROXY}</nonProxyHosts></proxy>\n"
        "  </proxies>\n"
        "</settings>\n")


def gradle_properties(host: str, port: int) -> str:
    return ("# Written by BOSS for this sandboxed run: every download goes through boss-egress.\n"
            f"systemProp.https.proxyHost={host}\nsystemProp.https.proxyPort={port}\n"
            f"systemProp.http.proxyHost={host}\nsystemProp.http.proxyPort={port}\n"
            f"systemProp.http.nonProxyHosts={NON_PROXY}\n"
            "org.gradle.daemon=false\n")


def _mkdirs(base: Path, rel: str) -> Path:
    """Directories the run's uid can write in (it is not the uid creating them): 2777 inside the
    run directory, which only that run and BOSS can enter."""
    path = base
    for part in Path(rel).parts:
        path = path / part
        if not path.exists():
            path.mkdir()
            os.chmod(path, 0o2777)
    return path


def write_proxy_config(home: Path, proxy_url: Optional[str]) -> bool:
    """Maven and Gradle ignore HTTPS_PROXY; give them the egress proxy in their own config
    files, in the run's HOME. No-op without Java or without a proxy."""
    if not has("java") or not proxy_url:
        return False
    hp = _proxy_host_port(proxy_url)
    if hp is None:
        return False
    for rel, text in ((".m2/settings.xml", maven_settings_xml(*hp)),
                      (".gradle/gradle.properties", gradle_properties(*hp))):
        target = _mkdirs(home, str(Path(rel).parent)) / Path(rel).name
        target.write_text(text, encoding="utf-8")
        os.chmod(target, 0o644)
    return True


def cache_root() -> Path:
    return Path(os.environ.get(CACHE_ROOT_ENV) or DEFAULT_CACHE_ROOT)


def project_cache_dir(project_id: str) -> Path:
    """One directory per project; the name is a hash, so a project id never becomes a path."""
    return cache_root() / hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:32]


def _copy_tree(src: Path, dst: Path, *, open_modes: bool) -> int:
    """Copy what is readable; skip what is not (the run's uid may chmod its own files). With
    `open_modes` every directory is 2777 and every file 0666: the run's uid is not the copier.
    Returns bytes copied."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel = Path(dirpath).relative_to(src)
        out = dst / rel
        out.mkdir(parents=True, exist_ok=True)
        if open_modes:
            os.chmod(out, 0o2777)
        for name in filenames:
            s = Path(dirpath) / name
            if s.is_symlink() or not s.is_file():
                continue
            try:
                shutil.copyfile(s, out / name)
                total += s.stat().st_size
            except OSError:
                continue
            os.chmod(out / name, 0o666 if open_modes else 0o600)
    return total


def restore_cache(home: Path, project_id: Optional[str]) -> list[str]:
    """Into a fresh run's HOME: this project's Maven/Gradle downloads from earlier runs."""
    if not project_id or not has("java"):
        return []
    base = project_cache_dir(project_id)
    restored = []
    for name, rel in CACHE_DIRS:
        src = base / name
        if not src.is_dir():
            continue
        try:
            parent = _mkdirs(home, str(Path(rel).parent))
            old = os.umask(0)
            try:
                _copy_tree(src, parent / Path(rel).name, open_modes=True)
            finally:
                os.umask(old)
            restored.append(name)
        except OSError as exc:
            log.warning("toolchain cache restore %s for %s failed: %s", name, project_id, exc)
    return restored


def save_cache(home: Path, project_id: Optional[str]) -> list[str]:
    """At run end, before the run directory goes: keep this project's downloads for its next run.
    Replaced whole (copy beside, then swap), so a concurrent restore never sees half a cache."""
    if not project_id or not has("java"):
        return []
    limit = int(os.environ.get(CACHE_MAX_ENV) or DEFAULT_CACHE_MAX_MB) * 1024 * 1024
    base = project_cache_dir(project_id)
    saved = []
    for name, rel in CACHE_DIRS:
        src = home / rel
        if not src.is_dir():
            continue
        try:
            base.mkdir(parents=True, exist_ok=True)
            os.chmod(base, 0o700)
            tmp = base / f".{name}.{os.getpid()}.new"
            shutil.rmtree(tmp, ignore_errors=True)
            size = _copy_tree(src, tmp, open_modes=False)
            if size > limit:
                shutil.rmtree(tmp, ignore_errors=True)
                log.warning("toolchain cache %s for %s is %d MB, over %s; not kept", name,
                            project_id, size // (1024 * 1024), CACHE_MAX_ENV)
                continue
            final, old = base / name, base / f".{name}.{os.getpid()}.old"
            if final.exists():
                os.rename(final, old)
            os.rename(tmp, final)
            shutil.rmtree(old, ignore_errors=True)
            saved.append(name)
        except OSError as exc:
            log.warning("toolchain cache save %s for %s failed: %s", name, project_id, exc)
    return saved
