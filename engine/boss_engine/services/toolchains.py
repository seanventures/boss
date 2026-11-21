"""PER-STACK TOOLCHAINS IN THE BUILD SANDBOX (docs/community/TOOLCHAINS.md).

The API/worker image always carries Python and Node. Anything else is chosen per install:
`BOSS_TOOLCHAINS` in .env (default `java,go` when the line is absent; `none`, `java`, `go`,
`security`, comma-separated) selects the `toolchains-java` (and `toolchains-go`) stages of
Dockerfile.community, which put a pinned, checksum-verified Temurin JDK, Maven and Gradle (a Go
release) under /opt/toolchains and write /opt/toolchains/manifest.json. An install with
`BOSS_TOOLCHAINS=none` carries an empty manifest and nothing else.

This module is the one place that knows:

* which toolchains this image has (`installed`, read from the manifest);
* which stack a checkout is (`detect_java`: pom.xml, build.gradle, build.gradle.kts;
  `detect_go`: go.mod);
* the commands that test and build it (`java_gate_commands`, `GO_GATE`) and run it
  (`java_preview_command`, `go_run_command`);
* what to say when the toolchain is not there (`missing_message`) — in words, never "mvn: not
  found";
* how a sandboxed run reaches Maven Central, the Gradle plugin portal and the Go module proxy
  (only through boss-egress's CONNECT proxy: `write_proxy_config`, `sandbox_env`), and how its
  dependency caches persist per
  PROJECT across runs without ever being handed to another project (`restore_cache`,
  `save_cache`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

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
         "go": "Go (1.27, go vet, go test)",
         "security": "Security scanners (Semgrep, OSV-Scanner, OWASP ZAP; includes Java)"}
ENABLE_HINT = "boss toolchains enable {name}"
#: What a person reads for each toolchain in a refusal ("needs Go").
LABELS = {"java": "Java", "go": "Go", "security": "the security scanners"}

MAVEN_FILES = ("pom.xml",)
GRADLE_FILES = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")

#: Batch mode, no transfer progress: a log a person (and the repair agent) can read.
MAVEN_GATE = ["mvn -B -ntp verify"]
#: The image's Gradle, not ./gradlew: the wrapper downloads a distribution from
#: services.gradle.org, which the sandbox's egress allowlist does not include.
GRADLE_GATE = ["gradle --no-daemon --console=plain build"]
GATE_TIMEOUT_SECONDS = 1200

GO_MANIFESTS = ("go.mod",)
#: Vet, then the tests (they compile every package). Two plain commands: the sandbox runs each
#: without a shell, so the gate is `go vet ./... && go test ./...` as two steps.
GO_GATE = ["go vet ./...", "go test ./..."]

#: Per-project caches, relative to the sandbox HOME: (cache name, path under HOME, toolchain).
#: Gradle's downloaded modules only — its transforms and daemons are per-machine scratch. Go's
#: module cache (GOMODCACHE) and its build cache (GOCACHE), where sandbox_env puts them.
CACHE_DIRS = (("m2", ".m2/repository", "java"), ("gradle", ".gradle/caches/modules-2", "java"),
              ("gomod", "go/pkg/mod", "go"), ("gobuild", ".cache/go-build", "go"))


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


def manifest_problem() -> Optional[str]:
    """Why this install's toolchain manifest cannot be read, or None when it reads as a JSON
    object. Every image writes one ({} with no toolchain on), so a missing, unreadable or
    malformed file means BOSS cannot tell which toolchains are on — never "it is off"."""
    path = manifest_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return f"{path}: {exc.strerror or type(exc).__name__}"
    except ValueError:
        return f"{path}: not valid JSON"
    if not isinstance(data, dict):
        return f"{path}: not a JSON object"
    return None


def parse_selection(value: Optional[str]) -> list[str]:
    """BOSS_TOOLCHAINS as a list: "java,go" -> ["java", "go"]; "none" or empty -> []. The same
    reading as the Dockerfile's and boss.sh's: comma-separated, no spaces, "none" alone."""
    out = []
    for part in str(value or "").split(","):
        part = part.strip().lower()
        if part and part != "none" and part not in out:
            out.append(part)
    return out


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


def enable_instructions(name: str) -> str:
    """How a person turns toolchain `name` on, in one clause."""
    return (f"`{ENABLE_HINT.format(name=name)}` on the BOSS host (or set "
            f"BOSS_TOOLCHAINS={name} in .env — comma-separated with any others, e.g. "
            f"BOSS_TOOLCHAINS=java,go — and run `boss update`)")


def missing_message(name: str, found: str) -> str:
    """The words a build shows when the project needs a toolchain this install does not have."""
    label = LABELS.get(name, name)
    problem = manifest_problem()
    if problem:
        return (f"This project needs {label} (it has a {found}), and BOSS could not read its "
                f"toolchain manifest ({problem}), so it cannot tell whether {label} is turned on. "
                f"Rebuild the images with `boss update`; if {label} is not on, turn it on with "
                f"{enable_instructions(name)}. Then run the build again. BOSS does not build it "
                "in another language instead.")
    return (f"This project needs {label} (it has a {found}), and {label} is not turned on for "
            f"this BOSS. Turn it on with {enable_instructions(name)}, then run the build again. "
            f"BOSS does not build it in another language instead.")


def unsupported_message(label: str, found: str) -> str:
    """The words a build shows for a project in a language BOSS cannot build at all."""
    return (f"This project is {label} (it has a {found}), which BOSS cannot build. BOSS does not "
            "build it in another language instead; see docs/community/SUPPORTED_STACKS.md.")


# ---- Go --------------------------------------------------------------------------------------

def detect_go(root: Path | str) -> bool:
    """Is the checkout's root a Go module (go.mod)?"""
    return any((Path(root) / name).is_file() for name in GO_MANIFESTS)


def go_main_package(app_root: Path | str) -> str:
    """The package `go build` makes the server from: the module root when it is `package main`,
    else the first ./cmd/<name> with a main.go."""
    base = Path(app_root)
    pkg_main = re.compile(r"^package\s+main\b", re.M)
    for f in sorted(base.glob("*.go")):
        if not f.name.endswith("_test.go") and pkg_main.search(_head(f)):
            return "."
    cmd = base / "cmd"
    if cmd.is_dir():
        for d in sorted(p for p in cmd.iterdir() if p.is_dir()):
            if (d / "main.go").is_file():
                return f"./cmd/{d.name}"
    return "."


def _head(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return ""


def go_run_command(app_root: Path | str) -> list[str]:
    """Build the server binary into the run's TMPDIR (never into the checkout), then run it. The
    app reads its port from $PORT (BOSS's contract for every backend)."""
    pkg = go_main_package(app_root)
    return ["sh", "-lc",
            f'out="${{TMPDIR:-/tmp}}/boss-go-app-$$" && go build -o "$out" {pkg} '
            f'&& exec "$out"']


def sandbox_env(home: Path | str) -> dict[str, str]:
    """The environment a sandboxed run gets for the toolchains this image carries. Go: modules
    only from the Go module proxy and checked against the checksum database, both reached
    through boss-egress (HTTPS_PROXY, which Go honours); caches under the run's HOME, where
    restore_cache/save_cache keep them per project; the image's pinned Go, never a toolchain
    download a go.mod `toolchain` line asks for; a module cache the run can delete."""
    if not has("go"):
        return {}
    from .enterprise_network import go_environment

    home = str(home)
    return {"GOPATH": f"{home}/go", "GOMODCACHE": f"{home}/go/pkg/mod",
            "GOCACHE": f"{home}/.cache/go-build",
            "GOTOOLCHAIN": "local", "GOFLAGS": "-modcacherw",
            # Nothing skips the proxy or the checksum database, whatever a project's own
            # environment or go.work says. Empty, not unset: an empty value overrides go env -w.
            # Not -mod=readonly: building a Go change means `go get` / `go mod tidy` updating
            # go.mod and go.sum, and every added module is still checked against GOSUMDB.
            **{name: "" for name in GO_PRIVATE_VARS},
            # The operator's module proxy and checksum database (BOSS_GOPROXY, BOSS_GOSUMDB) when
            # set, else the public ones; BOSS_GONOSUMDB is the operator's own list of internal
            # module paths (still fetched through the proxy, never directly from a VCS host).
            **go_environment(os.environ, default_proxy=GOPROXY, default_sumdb=GOSUMDB)}


#: The variables that would let a module skip GOPROXY or GOSUMDB (or fetch over plain HTTP).
GO_PRIVATE_VARS = ("GOPRIVATE", "GONOPROXY", "GONOSUMDB", "GONOSUMCHECK", "GOINSECURE")


#: The only module source (no `,direct`: a direct fetch would go to a VCS host the egress
#: allowlist does not include) and the checksum database every module is checked against.
GOPROXY = "https://proxy.golang.org"
GOSUMDB = "sum.golang.org"


def java_package_command(build: str) -> str:
    """Build the app's runnable jar without its tests (the gate ran them)."""
    if build == "maven":
        return "mvn -B -ntp -q -DskipTests package"
    return "gradle --no-daemon -q -x test assemble"


def java_jar_glob(build: str) -> str:
    return "target/*.jar" if build == "maven" else "build/libs/*.jar"


def java_pick_jar(build: str) -> str:
    """Shell that sets $jar to the runnable jar the package step wrote (never the -plain,
    original-, -sources or -javadoc jars beside it), or fails saying where it looked."""
    jars = java_jar_glob(build)
    return (f'jar=$(ls {jars} 2>/dev/null | grep -v -e "original-" -e "-plain.jar" '
            f'-e "-sources.jar" -e "-javadoc.jar" | head -n 1); '
            f'[ -n "$jar" ] || {{ echo "no jar under {jars.rsplit("/", 1)[0]}" >&2; exit 1; }}; ')


def java_config_file(app_root: Path) -> Optional[str]:
    """A Dropwizard app's config beside its build (it runs as `server config.yml`)."""
    return next((n for n in ("config.yml", "config.yaml") if (Path(app_root) / n).is_file()), None)


def java_run_args(config: Optional[str]) -> str:
    return f" server {config}" if config else ""


def java_preview_command(app_root: Path, build: str) -> list[str]:
    """Build the jar, then run it on $PORT. A Dropwizard app (a config.yml beside the build)
    runs as `server config.yml`; its config reads the port from ${PORT} (see TOOLCHAINS.md).
    Spring Boot takes -Dserver.port. Anything else declares boss.preview.command."""
    args = java_run_args(java_config_file(app_root))
    return ["sh", "-lc",
            f'{java_package_command(build)} && {java_pick_jar(build)}'
            f'exec java -Dserver.port="$PORT" -jar "$jar"{args}']


# ---- container images (preview and deploy, outside the BOSS image) -----------------------------

#: Where a Java app is BUILT when its image is made outside the BOSS image (a container preview,
#: the deploy host): the same JDK major and build tools the toolchains-java stage pins.
JAVA_BUILD_IMAGES = {"maven": "maven:3.9-eclipse-temurin-21",
                     "gradle": "gradle:8.14-jdk21"}
#: Where it RUNS: a JRE, no build tools.
JAVA_RUNTIME_IMAGE = "eclipse-temurin:21-jre"
#: The port a generated Java image listens on (Spring Boot's own default).
JAVA_PORT = 8080


def java_dockerfile(build: str, app_dir: str = ".", *, config: Optional[str] = None,
                    port: int = JAVA_PORT) -> str:
    """A two-stage Dockerfile for a Java app at `app_dir` (relative to the build context): the
    jar is built with the build tool's image and run on a JRE, on $PORT like every BOSS image.
    Dropwizard (`config`) reads its port from ${PORT} in its own config."""
    if build not in JAVA_BUILD_IMAGES:
        raise ValueError(f"not a Java build: {build!r}")
    src = "/src" if app_dir in (".", "") else f"/src/{app_dir.strip('/')}"
    copy_config = f"COPY --from=build {src}/{config} /app/{config}\n" if config else ""
    return (
        f"FROM {JAVA_BUILD_IMAGES[build]} AS build\n"
        "COPY . /src\n"
        f"WORKDIR {src}\n"
        f"RUN {java_package_command(build)} && {java_pick_jar(build)}cp \"$jar\" /tmp/app.jar\n"
        f"FROM {JAVA_RUNTIME_IMAGE}\n"
        "WORKDIR /app\n"
        "COPY --from=build /tmp/app.jar /app/app.jar\n"
        f"{copy_config}"
        f"ENV HOST=0.0.0.0 PORT={port}\nEXPOSE {port}\n"
        f'CMD ["sh", "-c", "exec java -Dserver.port=$PORT -jar /app/app.jar'
        f'{java_run_args(config)}"]\n')


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


def maven_settings_xml(host: str, port: int, mirror: str = "") -> str:
    """Maven's settings for a sandboxed run. `mirror` (BOSS_MAVEN_MIRROR, already validated:
    https, no credentials) replaces every remote repository a project names."""
    mirrors = ""
    if mirror:
        mirrors = ("  <mirrors>\n"
                   "    <mirror><id>boss-enterprise-mirror</id><mirrorOf>*</mirrorOf>"
                   f"<url>{xml_escape(mirror)}</url></mirror>\n"
                   "  </mirrors>\n")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- Written by BOSS for this sandboxed run: every download goes through boss-egress. -->\n"
        '<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">\n'
        f"{mirrors}"
        "  <proxies>\n"
        f"    <proxy><id>boss-egress</id><active>true</active><protocol>https</protocol>"
        f"<host>{host}</host><port>{port}</port><nonProxyHosts>{NON_PROXY}</nonProxyHosts></proxy>\n"
        f"    <proxy><id>boss-egress-http</id><active>true</active><protocol>http</protocol>"
        f"<host>{host}</host><port>{port}</port><nonProxyHosts>{NON_PROXY}</nonProxyHosts></proxy>\n"
        "  </proxies>\n"
        "</settings>\n")


def gradle_properties(host: str, port: int, trust_store: str = "") -> str:
    trust = (f"systemProp.javax.net.ssl.trustStore={trust_store}\n"
             "systemProp.javax.net.ssl.trustStorePassword=changeit\n") if trust_store else ""
    return ("# Written by BOSS for this sandboxed run: every download goes through boss-egress.\n"
            f"systemProp.https.proxyHost={host}\nsystemProp.https.proxyPort={port}\n"
            f"systemProp.http.proxyHost={host}\nsystemProp.http.proxyPort={port}\n"
            f"systemProp.http.nonProxyHosts={NON_PROXY}\n"
            f"{trust}"
            "org.gradle.daemon=false\n")


#: The JVM trust store with the install's extra CA added (scripts/install/with-extra-ca.sh
#: writes it at container start when BOSS_EXTRA_CA_PEM_B64 is set and the image has a JDK).
#: Java ignores SSL_CERT_FILE, so Maven and Gradle are pointed at it in their own config.
JAVA_TRUST_STORE_ENV = "BOSS_JAVA_TRUST_STORE"


def java_trust_store() -> str:
    path = (os.environ.get(JAVA_TRUST_STORE_ENV) or "").strip()
    return path if path and os.path.isfile(path) else ""


def mavenrc(trust_store: str) -> str:
    return ("# Written by BOSS for this sandboxed run: the install's extra CA for Maven's JVM.\n"
            f'MAVEN_OPTS="$MAVEN_OPTS -Djavax.net.ssl.trustStore={trust_store} '
            '-Djavax.net.ssl.trustStorePassword=changeit"\n')


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
    from .enterprise_network import maven_mirror

    trust = java_trust_store()
    files = [(".m2/settings.xml", maven_settings_xml(*hp, mirror=maven_mirror(os.environ))),
             (".gradle/gradle.properties", gradle_properties(*hp, trust_store=trust))]
    if trust:
        files.append((".mavenrc", mavenrc(trust)))
    for rel, text in files:
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
    `open_modes` every directory is 2777 and every file 0666 (0777 when executable): the run's uid
    is not the copier.
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
                st = s.stat()
                total += st.st_size
            except OSError:
                continue
            # An executable stays one (a Gradle wrapper's bin/gradle): workspace_perms.usable_mode
            # for the run's uid, owner-only (0700) in BOSS's own cache.
            from . import workspace_perms

            own = 0o700 if st.st_mode & 0o111 else 0o600
            os.chmod(out / name, workspace_perms.usable_mode(own, is_dir=False) if open_modes else own)
    return total


def _caches() -> list[tuple[str, str]]:
    """(name, path under HOME) of every cache whose toolchain this image carries."""
    return [(name, rel) for name, rel, tool in CACHE_DIRS if has(tool)]


def restore_cache(home: Path, project_id: Optional[str]) -> list[str]:
    """Into a fresh run's HOME: this project's Maven/Gradle/Go downloads from earlier runs."""
    caches = _caches()
    if not project_id or not caches:
        return []
    base = project_cache_dir(project_id)
    restored = []
    for name, rel in caches:
        src = base / name
        if not src.is_dir():
            continue
        try:
            parent = _mkdirs(home, str(Path(rel).parent))
            # open_modes chmods every entry itself; never os.umask (process-wide).
            _copy_tree(src, parent / Path(rel).name, open_modes=True)
            restored.append(name)
        except OSError as exc:
            log.warning("toolchain cache restore %s for %s failed: %s", name, project_id, exc)
    return restored


def save_cache(home: Path, project_id: Optional[str]) -> list[str]:
    """At run end, before the run directory goes: keep this project's downloads for its next run.
    Replaced whole (copy beside, then swap), so a concurrent restore never sees half a cache."""
    caches = _caches()
    if not project_id or not caches:
        return []
    limit = int(os.environ.get(CACHE_MAX_ENV) or DEFAULT_CACHE_MAX_MB) * 1024 * 1024
    base = project_cache_dir(project_id)
    saved = []
    for name, rel in caches:
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
