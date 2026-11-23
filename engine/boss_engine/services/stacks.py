"""WHAT BOSS CAN BUILD — the one source of truth, and the per-service plan that builds it.

WHY THIS EXISTS. A workflow applied the login-oauth template on the Python stack and asked for a
FastAPI backend plus a React/Vite frontend in `helpdesk-ai/` (crun_78d197f7614f40f38993). The PRD
and the architecture were approved; the build then spent an hour and $26 failing, because the
build pipeline knew one shape of app with a UI: ONE npm package. The test author put a
Playwright harness at `helpdesk-ai/`, the gate ran `npm install && npm run build && … && npm run
test:e2e` there, the Python API was never started (every auth route answered 404), the web
package contract complained about vite, and the template's pytest suite never ran. And nothing
before the build could have said "BOSS cannot build that": a brief asking for Go was accepted at
Start, argued over by a panel, designed by an architect and approved by a person, and refused only
by the build's toolchain.

This module is both halves of the fix.

* THE REGISTRY. `LANGUAGES` is the build code itself: each entry carries the functions that
  install, build, unit-test and start a service in that language (`_python_steps`,
  `_node_steps`, `_java_steps`, `_go_steps`, `_start_*`). A language is BUILDABLE because it has
  those functions here, and READY because its toolchain is in this image
  (`toolchains.installed()`): Python and Node are always in the image, Java and Go only when the
  install turned them on. Nothing else
  keeps a list. `capabilities()` is what the API, the console, the panel's prompt and the
  architect's prompt all read; `check_text()` / `check_build()` / `check_architecture()` are the
  refusals Start, architecture approval and the build give, in words, naming what IS supported
  and how to turn a toolchain on when it is merely off.

* THE PLAN. `detect_services()` reads an app directory's manifests (requirements.txt /
  pyproject.toml, package.json, pom.xml / build.gradle, go.mod) and says which services it holds and
  which role each plays (backend, frontend, an e2e harness). When an app is MORE THAN ONE
  service, `stack_plan()` gives each its own install, build and unit tests in its own toolchain
  (pytest, npm scripts, Maven/Gradle), then names the backend's real start command and the
  e2e commands that run against it once it is up (services.stack_verify runs them). A
  single-service app never gets here: `verification_target.resolve_gate_plan` keeps today's
  behaviour for it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional

from . import toolchains

#: Never a service directory, and never searched for one.
SKIP_DIRS = frozenset({"node_modules", ".git", ".venv", "venv", "env", "__pycache__", "dist",
                       "build", ".next", "target", ".pytest_cache", "coverage", ".gradle",
                       "tests", "test", "e2e", "docs", "scripts", ".github", "migrations",
                       "public", "static", "src",
                       # Test and tool OUTPUT: never a service, and a test run leaves it
                       # readable only by the sandbox uid (crun_2b10d03d, crun_1412506d died
                       # stat-ing test-results/go.mod).
                       "test-results", "playwright-report", "blob-report", ".vite", ".turbo",
                       ".cache", "out", "tmp"})
#: Directories that hold services one level further down (apps/api, services/web).
GROUP_DIRS = ("apps", "services", "packages")
#: More services than this under one directory is a monorepo of apps, not one app.
MAX_SERVICES = 4

PYTHON_MANIFESTS = ("requirements.txt", "pyproject.toml", "setup.py", "Pipfile")
NODE_MANIFESTS = ("package.json",)
JAVA_MANIFESTS = toolchains.MAVEN_FILES + toolchains.GRADLE_FILES
GO_MANIFESTS = toolchains.GO_MANIFESTS

PIP_INSTALL = ("python3 -m pip install --user --disable-pip-version-check --no-input -q "
               "-r requirements.txt")
PIP_INSTALL_PROJECT = ("python3 -m pip install --user --disable-pip-version-check --no-input -q .")
PYTEST = "python3 -m pytest -q"
#: The npm scripts a service runs on its own, in this order, before anything is started.
NODE_UNIT_SCRIPTS = ("typecheck", "lint", "build", "test", "test:unit")
#: The npm scripts that need the backend up (run in the e2e phase, in this order).
NODE_E2E_SCRIPTS = ("test:api", "test:e2e")
#: The test script `npm init` writes. Not a test.
_NPM_PLACEHOLDER_TEST = re.compile(r"no test specified", re.I)

#: The environment the e2e phase (and a template's tests) see: where the started backend is.
BACKEND_ENV = ("BOSS_BACKEND_URL", "BACKEND_URL", "API_BASE_URL")


# ── The registry ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Framework:
    key: str
    label: str
    role: str                      # "backend" or "frontend"
    #: Dependency names (requirements/pyproject/package.json/pom) that identify it.
    markers: tuple[str, ...]
    #: Words a brief or an architecture uses for it.
    words: tuple[str, ...] = ()
    #: How a backend starts on $PORT (`{target}`: the app object the code names), or how BOSS
    #: serves a built front end on $PORT when its Playwright config serves nothing. "" when the
    #: language's own rule decides (npm start, the jar).
    start: str = ""


@dataclass(frozen=True)
class Language:
    key: str
    label: str
    #: None: always in the image. Otherwise the toolchain (services.toolchains.KNOWN) it needs.
    toolchain: Optional[str]
    manifests: tuple[str, ...]
    frameworks: tuple[Framework, ...]
    #: (service dir) -> [(command, dir)]: install, build and unit tests in its own toolchain.
    steps: Callable[["Service", Path], list[tuple[str, Path]]]
    #: (service dir) -> argv that starts it on $PORT, or None when it is not a server.
    start: Callable[["Service", Path], Optional[list[str]]]
    words: tuple[str, ...] = ()
    #: The runtime the image carries for it, and its package manager.
    runtime: str = ""
    package_manager: str = ""
    #: install / build / lint / typecheck / test / e2e — THE commands `steps` runs (None: not
    #: a separate step for this language). The adapter registry and the docs read them here.
    commands: Mapping[str, Optional[str]] = field(default_factory=dict)
    #: Regexes that name the language in a brief, for a language whose name is also an
    #: everyday word ("Go": "go to", "go live" are not requests). Read with `words`.
    patterns: tuple[str, ...] = ()
    #: Regexes for the language's own tooling ("go vet", "go test ./..."). A mention of the
    #: tooling is a request only in a sentence that states a requirement ("… clean", "must
    #: pass"): "our CLI runs `go test ./...` separately" asks for nothing.
    tooling_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Unsupported:
    """A language BOSS recognises in a brief and cannot build. Only for saying so in words."""
    key: str
    label: str
    patterns: tuple[str, ...]


@dataclass
class Service:
    rel: str                       # relative to the app directory ("." for the app itself)
    language: str                  # a LANGUAGES key, or an Unsupported key
    framework: Optional[str]
    role: str                      # backend | frontend | e2e | unknown
    supported: bool = True

    @property
    def name(self) -> str:
        return PurePosixPath(self.rel).name if self.rel != "." else "app"

    def as_dict(self) -> dict[str, Any]:
        return {"dir": self.rel, "language": self.language, "framework": self.framework,
                "role": self.role, "supported": self.supported}


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _package(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_read(path / "package.json"))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _deps(pkg: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        value = pkg.get(key)
        if isinstance(value, dict):
            out.update(str(k) for k in value)
    return out


def _scripts(pkg: Mapping[str, Any]) -> dict[str, str]:
    value = pkg.get("scripts")
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


# -- Python ---------------------------------------------------------------------------------

def _py_files(base: Path, depth: int = 3) -> list[Path]:
    out: list[Path] = []
    stack = [(base, 0)]
    while stack:
        d, level = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_symlink():
                continue
            if p.is_dir():
                if level < depth and p.name not in SKIP_DIRS - {"src"} and not p.name.startswith("."):
                    stack.append((p, level + 1))
            elif p.suffix == ".py" and not p.name.startswith("test_") and p.name != "conftest.py":
                out.append(p)
    # The conventional entry files first, shallow before deep.
    rank = {"main.py": 0, "app.py": 1, "server.py": 2, "asgi.py": 3, "wsgi.py": 4, "api.py": 5}
    return sorted(out, key=lambda p: (len(p.relative_to(base).parts), rank.get(p.name, 9), str(p)))


def _module_of(base: Path, path: Path) -> str:
    rel = path.relative_to(base).with_suffix("")
    return ".".join(rel.parts)


def _find_app_object(base: Path, constructor: str) -> Optional[tuple[str, str, bool]]:
    """(module, attribute, is_factory) of the first `x = FastAPI(...)` / `x = Flask(...)` in
    the service, or of a create_app() factory that builds one."""
    instance = re.compile(rf"^(\w+)\s*(?::[^=\n]+)?=\s*{constructor}\(", re.M)
    factory = re.compile(r"^def (create_app|make_app|build_app|get_app)\s*\(", re.M)
    found_factory: Optional[tuple[str, str, bool]] = None
    for path in _py_files(base):
        text = _read(path)
        if f"{constructor}(" not in text:
            continue
        m = instance.search(text)
        if m:
            return _module_of(base, path), m.group(1), False
        f = factory.search(text)
        if f and found_factory is None:
            found_factory = (_module_of(base, path), f.group(1), True)
    return found_factory


def _has_python_tests(base: Path) -> bool:
    for pattern in ("test_*.py", "*_test.py"):
        for p in base.rglob(pattern):
            if not any(part in {"node_modules", ".venv", "venv", "site-packages", ".git"}
                       for part in p.parts):
                return True
    return False


def _python_steps(service: Service, path: Path) -> list[tuple[str, Path]]:
    commands = LANGUAGES["python"].commands
    steps: list[tuple[str, Path]] = []
    if (path / "requirements.txt").is_file():
        steps.append((str(commands["install"]), path))
    elif "[project]" in _read(path / "pyproject.toml") or (path / "setup.py").is_file():
        steps.append((PIP_INSTALL_PROJECT, path))
    if _has_python_tests(path):
        steps.append((str(commands["test"]), path))
    return steps


FASTAPI_START = 'exec python3 -m uvicorn {target} --host 0.0.0.0 --port "$PORT"'
FLASK_START = 'exec python3 -m flask --app {target} run --host 0.0.0.0 --port "$PORT"'
DJANGO_START = 'exec python3 manage.py runserver "0.0.0.0:$PORT" --noreload'
VITE_SERVE = 'exec npm run preview -- --host 127.0.0.1 --port "$PORT" --strictPort'
NEXT_SERVE = 'exec npm run start -- -H 127.0.0.1 -p "$PORT"'


def _start_python(service: Service, path: Path) -> Optional[list[str]]:
    if service.framework == "django" or (path / "manage.py").is_file():
        return ["sh", "-lc", DJANGO_START]
    if service.framework == "flask":
        found = _find_app_object(path, "Flask")
        target = (found[0] if found and found[2] else f"{found[0]}:{found[1]}") if found else "app"
        return ["sh", "-lc", FLASK_START.format(target=target)]
    found = _find_app_object(path, "FastAPI")
    if found:
        module, attr, is_factory = found
        target = f"{module}:{attr}" + (" --factory" if is_factory else "")
        return ["sh", "-lc", FASTAPI_START.format(target=target)]
    for name in ("main.py", "app.py", "server.py"):
        if (path / name).is_file():
            return ["sh", "-lc", f"exec python3 {name}"]
    return None


# -- Node -----------------------------------------------------------------------------------

def _node_steps(service: Service, path: Path) -> list[tuple[str, Path]]:
    scripts = _scripts(_package(path))
    steps: list[tuple[str, Path]] = [(str(LANGUAGES["node"].commands["install"]), path)]
    for name in NODE_UNIT_SCRIPTS:
        body = scripts.get(name, "").strip()
        if not body or (name == "test" and _NPM_PLACEHOLDER_TEST.search(body)):
            continue
        steps.append((f"npm run {name}", path))
    return steps


def _start_node(service: Service, path: Path) -> Optional[list[str]]:
    pkg = _package(path)
    scripts = _scripts(pkg)
    if scripts.get("start", "").strip():
        return ["sh", "-lc", "exec npm start"]
    main = str(pkg.get("main") or "").strip()
    for candidate in ([main] if main else []) + ["server.js", "server.mjs", "index.js", "index.mjs"]:
        if candidate and (path / candidate).is_file():
            return ["sh", "-lc", f"exec node {candidate}"]
    return None


# -- Java -----------------------------------------------------------------------------------

def _java_steps(service: Service, path: Path) -> list[tuple[str, Path]]:
    build = toolchains.detect_java(path)
    return [(c, path) for c in toolchains.java_gate_commands(build)] if build else []


def _start_java(service: Service, path: Path) -> Optional[list[str]]:
    build = toolchains.detect_java(path)
    return toolchains.java_preview_command(path, build) if build else None


# -- Go -------------------------------------------------------------------------------------

def _go_steps(service: Service, path: Path) -> list[tuple[str, Path]]:
    return [(c, path) for c in toolchains.GO_GATE] if toolchains.detect_go(path) else []


def _start_go(service: Service, path: Path) -> Optional[list[str]]:
    return toolchains.go_run_command(path) if toolchains.detect_go(path) else None


#: "Go" in a brief. The name is an everyday verb, so only phrasings that can only mean the
#: language: "(Go)", "(Go, medium complexity)", "idiomatic Go", "a Go service", "written in Go",
#: "go vet", "go test ./...", go.mod — never "go to", "go live", "go-to-market".
_GO_NOT_LANGUAGE = (r"(?![\s-]*(?:to|back|ahead|live|through|for|into|out|over|away|and|or|"
                    r"beyond|further|wrong|down|up|public|here|there|on|off|with|by)\b)")
#: After a preposition that already says "language" ("in Go", "to Go" after a migration verb),
#: only the idioms "go back", "go live", "go ahead"… are not the language: "in Go to serve the
#: app", "to Go for the gateway", "in Go with Gin" are.
_GO_NOT_LANGUAGE_AFTER_PREP = (r"(?![\s-]*(?:back|ahead|live|through|out|over|away|beyond|"
                               r"further|wrong|down|up|public|here|there|off)\b)")
GO_PATTERNS = (
    r"\bgolang\b",
    r"\bgo\s+(?:backend|service|server|api|microservices?|modules?|binary|binaries|app|"
    r"application|code|programming|language|cli|library|package|toolchain|gateway|proxy|"
    r"daemon|codebase|project|worker|workers|tool|program|1\.\d+)\b",
    r"\b(?:in|using|written in|built in|build in|idiomatic|plain|pure|modern)\s+go\b"
    + _GO_NOT_LANGUAGE_AFTER_PREP,
    r"\b(?:with|on)\s+go\b" + _GO_NOT_LANGUAGE,
    # A parenthesised language tag: "(Go)", "(Go, medium complexity)", "(Go/Gin)".
    r"\(\s*go\s*(?:[,;/)]|\s+\d)",
    r"\bgo\s*(?:\+|/|and)\s*(?:react|vue|svelte|htmx)\b",
    r"\bgin(?:-gonic)?\s+(?:framework|router)\b",
    # Go as the TARGET of a migration: "port the Node service to Go".
    r"\b(?:port\w*|re-?writ\w*|migrat\w*|convert\w*|mov(?:e|es|ed|ing)|translat\w*)\b"
    r"[^.;\n]{0,60}?\b(?:to|into)\s+go\b" + _GO_NOT_LANGUAGE_AFTER_PREP)
#: The go command and its files: "go vet", "go test ./...", "go build ./cmd/x", "go mod tidy",
#: go.mod. Requests only in a sentence that states a requirement (Language.tooling_patterns).
GO_TOOLING_PATTERNS = (
    r"\bgo\s+(?:vet|fmt|mod\s+(?:init|tidy|download|vendor))\b",
    r"\bgo\s+(?:test|build|run|install|generate)\s+(?:-[\w=.]+\s+)*\.{1,3}/",
    r"`go\s+(?:test|build|run)\b", r"\bgofmt\b", r"\bgo\.mod\b")


def _npm_install() -> str:
    from .verification_target import NPM_INSTALL_CMD

    return NPM_INSTALL_CMD


#: THE REGISTRY. What BOSS can build is exactly what has build functions here.
LANGUAGES: dict[str, Language] = {
    "python": Language(
        "python", "Python", None, PYTHON_MANIFESTS,
        (Framework("fastapi", "FastAPI", "backend", ("fastapi",), ("fastapi",),
                   FASTAPI_START.format(target="<module>:<app>")),
         Framework("flask", "Flask", "backend", ("flask",), ("flask",),
                   FLASK_START.format(target="<module>:<app>")),
         Framework("django", "Django", "backend", ("django",), ("django",), DJANGO_START),
         # The standard library's wsgiref: no dependency to detect it by; it starts from its
         # declared command or its main.py.
         Framework("wsgi", "WSGI", "backend", (), ())),
        _python_steps, _start_python, ("python",),
        runtime="python >=3.11", package_manager="pip",
        commands={"install": PIP_INSTALL, "build": None, "lint": None, "typecheck": None,
                  "test": PYTEST, "e2e": None}),
    "node": Language(
        "node", "Node", None, NODE_MANIFESTS,
        (Framework("express", "Express", "backend", ("express",), ("express.js", "expressjs")),
         Framework("fastify", "Fastify", "backend", ("fastify",), ("fastify",)),
         Framework("koa", "Koa", "backend", ("koa",), ()),
         Framework("hono", "Hono", "backend", ("hono",), ()),
         # Node's own http module: a `start` script, no framework dependency.
         Framework("node-http", "node:http", "backend", (), ()),
         Framework("nestjs", "NestJS", "backend", ("@nestjs/core",), ("nestjs",)),
         # Next.js before React: a Next app depends on react too, and is served by `next start`.
         Framework("next", "Next.js", "frontend", ("next",), ("next.js", "nextjs"), NEXT_SERVE),
         Framework("react", "React", "frontend", ("react",), ("react",), VITE_SERVE),
         Framework("vite", "Vite", "frontend", ("vite",), ("vite",), VITE_SERVE),
         Framework("vue", "Vue", "frontend", ("vue",), ("vue",), VITE_SERVE),
         Framework("svelte", "Svelte", "frontend", ("svelte",), ("svelte", "sveltekit"), VITE_SERVE),
         Framework("angular", "Angular", "frontend", ("@angular/core",), ("angular",))),
        _node_steps, _start_node, ("node", "node.js", "nodejs", "typescript", "javascript"),
        runtime="node >=20", package_manager="npm",
        commands={"install": _npm_install(), "build": "npm run build", "lint": "npm run lint",
                  "typecheck": "npm run typecheck", "test": "npm run test",
                  "e2e": "npm run test:e2e"}),
    "java": Language(
        "java", "Java", "java", JAVA_MANIFESTS,
        (Framework("spring-boot", "Spring Boot", "backend", ("spring-boot",),
                   ("spring boot", "spring-boot", "springboot"),
                   'java -Dserver.port="$PORT" -jar <the built jar>'),
         Framework("dropwizard", "Dropwizard", "backend", ("dropwizard",), ("dropwizard",),
                   'java -jar <the built jar> server config.yml')),
        _java_steps, _start_java, ("java",),
        runtime="java 21 (Temurin)", package_manager="maven or gradle",
        commands={"install": None, "build": toolchains.MAVEN_GATE[0] + " | "
                  + toolchains.GRADLE_GATE[0], "lint": None, "typecheck": None,
                  "test": toolchains.MAVEN_GATE[0] + " | " + toolchains.GRADLE_GATE[0],
                  "e2e": None}),
    "go": Language(
        "go", "Go", "go", GO_MANIFESTS,
        (Framework("gin", "Gin", "backend", ("github.com/gin-gonic/gin",), ()),
         Framework("echo", "Echo", "backend", ("github.com/labstack/echo",), ()),
         Framework("chi", "chi", "backend", ("github.com/go-chi/chi",), ()),
         Framework("fiber", "Fiber", "backend", ("github.com/gofiber/fiber",), ()),
         # The standard library's net/http: no dependency to detect it by.
         Framework("net-http", "net/http", "backend", (), ())),
        _go_steps, _start_go, ("golang",),
        runtime="go 1.27", package_manager="go modules",
        commands={"install": None, "build": None, "lint": toolchains.GO_GATE[0],
                  "typecheck": None, "test": toolchains.GO_GATE[1], "e2e": None},
        patterns=GO_PATTERNS, tooling_patterns=GO_TOOLING_PATTERNS),
}

#: Languages BOSS recognises in a brief or an architecture and cannot build. For the words only.
UNSUPPORTED: tuple[Unsupported, ...] = (
    Unsupported("rust", "Rust", (r"\brust\b(?!-)", r"\b(?:actix|axum)\b", r"\bcargo\.toml\b")),
    Unsupported("ruby", "Ruby", (r"\bruby\b", r"\bruby on rails\b",
                                 r"\brails\s+(?:app|api|backend|server|application)\b",
                                 r"\bgemfile\b")),
    Unsupported("php", "PHP", (r"\bphp\b", r"\blaravel\b", r"\bsymfony\b")),
    Unsupported("dotnet", ".NET (C#)", (r"(?<![\w.])\.net\b(?=\s|$|,|\)|/)", r"\basp\.net\b",
                                       r"\bdotnet\b", r"\bc#", r"\bcsharp\b")),
    Unsupported("elixir", "Elixir", (r"\belixir\b", r"\bphoenix\s+(?:framework|liveview)\b")),
    Unsupported("scala", "Scala", (r"\bscala\b",)),
)
_UNSUPPORTED_MANIFESTS = {"Cargo.toml": "rust", "Gemfile": "ruby",
                          "composer.json": "php", "mix.exs": "elixir", "build.sbt": "scala"}
_NEGATION = re.compile(r"\b(?:not|no|never|without|instead of|avoid|rather than|except|"
                       r"don'?t use|do not use|rejected|considered)\b[^.,;\n]{0,24}$", re.I)
#: A list under a negation: "has no node, npm, pip3, java, go, ruby, php" names nothing it wants.
_NEGATED_LIST = re.compile(r"\b(?:not|no|never|without|lacks?)\s+(?:[\w.+#/-]+\s*,\s*){1,15}"
                           r"(?:and\s+|or\s+)?$", re.I)
#: An inventory of tool names (a JSON array of three or more bare words, as a spec's allowed
#: commands are dumped: ["bazel", "bundle", "cargo", …, "ruby"]) is not a request for any.
_NAME_INVENTORY = re.compile(r"\[\s*\"[\w.+-]{1,24}\"(?:\s*,\s*\"[\w.+-]{1,24}\"){2,}\s*\]")
#: …unless its key says it IS the stack: `"stack": ["go", "react", "postgres"]` is a request.
_STACK_KEY = re.compile(r"[\"']?(?:stacks?|languages?|frameworks?|runtimes?|backend|frontend|"
                        r"required_stack)[\"']?\s*[:=]\s*$", re.I)
#: A language is the SOURCE of a migration only in these shapes, the language right after the
#: verb (and its determiners), or right after "from" in a sentence with such a verb:
#:   "Rewrite our Go codebase in Node", "Port our Go service to Python (FastAPI)" — the verb, X,
#:   then to/into/in and ANOTHER language directly;
#:   "Migrate from Java to Go", "move away from Python" — "from X" after a migration verb;
#:   "to replace our Python one", "retire the Java service" — X is what goes away.
#: Anything else names X as wanted: "Build a Go service to replace the legacy Java one" asks for
#: Go (and names Java only as what it replaces).
_MIGRATION_VERBS = (r"re-?writ\w*|port(?:s|ed|ing)?|migrat\w*|convert\w*|translat\w*|"
                    r"mov(?:e|es|ed|ing)|switch\w*")
_DETERMINERS = r"(?:(?:our|the|this|that|its|their|my|existing|current|legacy|old|whole|entire|a|an)\s+){0,3}"
_SOURCE_AFTER_VERB = re.compile(rf"\b(?:{_MIGRATION_VERBS})\s+{_DETERMINERS}$", re.I)
_SOURCE_AFTER_FROM = re.compile(rf"\b(?:{_MIGRATION_VERBS}|away)\b[^.;\n]{{0,40}}?\bfrom\s+"
                                rf"{_DETERMINERS}$", re.I)
_REPLACED = re.compile(rf"\b(?:replac\w*|retir\w*|deprecat\w*|sunset\w*)\s+{_DETERMINERS}$", re.I)
_TARGET_LEAD = re.compile(r"^\s*(?:(?:code(?:base)?|service|services|app|application|api|backend|"
                          r"server|project|monolith|stack|implementation|version|one)\s+){0,2}"
                          r"(?:to|into|in)\s+(?:(?:the|a|an|plain|modern|idiomatic)\s+)?", re.I)
#: A sentence that states a requirement (for tooling mentions).
_REQUIREMENT = re.compile(r"\b(?:must|clean|pass(?:es|ing)?|green|requires?|required|should|"
                          r"needs? to|has to|have to)\b", re.I)
#: Where a sentence ends: never inside a code span or a path ("`go test ./...` must pass").
_SENTENCE_END = re.compile(r"(?<![./])[.!?;](?=\s|$)|\n")
_CODE_SPAN = re.compile(r"`[^`\n]*`")


# ── The adapter registry (BOSS_OSS_v1 stack recommendations §6-7) ───────────────────────────
#
# An ADAPTER is a stack BOSS builds end to end: which languages and frameworks it covers, at what
# support level. Only the choice of components and the level are written here; everything else an
# adapter says — runtimes, package managers, detection files, the install/build/lint/typecheck/
# test/e2e commands, how the backend starts and where its health is asked — is read from LANGUAGES,
# the same entries the gate plans and runs with. docs/community/SUPPORTED_STACKS.md is rendered
# from it (render_supported_stacks_md) and a test holds the file to it.

# The vocabulary is shared with the infrastructure plugin registry (support_levels.py).
from ..support_levels import STACK_LEVELS as SUPPORT_LEVELS  # noqa: E402
from ..support_levels import WORDS as _LEVEL_WORDS  # noqa: E402

LEVEL_WORDS = {k: _LEVEL_WORDS[k] for k in SUPPORT_LEVELS}
#: How an adapter is promoted. A person changes the level in ADAPTERS; nothing promotes itself.
PROMOTION_RULE = (
    "An experimental adapter becomes certified only when a recorded benchmark run for its id "
    "(GET /v1/delivery-workflows/runs/{run_id}/benchmark) passed end to end: result success, "
    "tests_passed equal to tests_total and above zero, and deployment_smoke_test pass. A "
    "maintainer then changes its support_level here, citing that run.")
DATABASES = ("postgres", "sqlite (local development)")


@dataclass(frozen=True)
class Component:
    role: str                      # backend | frontend
    language: str
    frameworks: tuple[str, ...]


@dataclass(frozen=True)
class Adapter:
    id: str
    label: str
    support_level: str
    components: tuple[Component, ...]
    databases: tuple[str, ...] = DATABASES
    note: str = ""

    @property
    def languages(self) -> list[str]:
        return list(dict.fromkeys(c.language for c in self.components))

    @property
    def frameworks(self) -> list[str]:
        return list(dict.fromkeys(f for c in self.components for f in c.frameworks))

    def ready(self) -> bool:
        return all(language_ready(k) for k in self.languages)

    def as_dict(self, *, install: bool = True) -> dict[str, Any]:
        """The adapter, every field derived from LANGUAGES. `install`: add whether this
        install's toolchains can build it (left out of the docs, which are not per install)."""
        comps = []
        for c in self.components:
            lang = LANGUAGES[c.language]
            fws = [f for f in lang.frameworks if f.key in c.frameworks]
            comps.append({
                "role": c.role, "language": lang.key, "frameworks": [f.key for f in fws],
                "runtime": lang.runtime, "package_manager": lang.package_manager,
                "detection_files": list(lang.manifests),
                "commands": dict(lang.commands),
                **({"start": next((f.start for f in fws if f.start), "") or
                    _DEFAULT_START.get(lang.key, ""),
                    "health_path": "/ (or the path in boss.preview.health)"}
                   if c.role == "backend" else
                   {"serve": next((f.start for f in fws if f.start), VITE_SERVE)}),
            })
        out = {"id": self.id, "label": self.label, "support_level": self.support_level,
               "languages": self.languages, "frameworks": self.frameworks,
               "databases": list(self.databases), "components": comps,
               **({"note": self.note} if self.note else {})}
        if install:
            out["ready"] = self.ready()
            out["enable"] = next((enable_hint(k) for k in self.languages if enable_hint(k)), None)
        return out


#: How a backend starts when its framework names no template (the language's own rule).
_DEFAULT_START = {"node": "exec npm start", "python": "exec python3 main.py",
                  "java": "java -jar <the built jar>",
                  "go": 'go build -o <binary> <main package> && exec <binary> (on "$PORT")'}

_REACT_VITE = Component("frontend", "node", ("react", "vite"))
ADAPTERS: tuple[Adapter, ...] = (
    Adapter("react-vite+express+postgres", "React + Vite, Express, PostgreSQL", "experimental",
            (_REACT_VITE, Component("backend", "node", ("express",)))),
    Adapter("react-vite+fastapi+postgres", "React + Vite, FastAPI, PostgreSQL", "experimental",
            (_REACT_VITE, Component("backend", "python", ("fastapi",)))),
    Adapter("nextjs+fastapi+postgres", "Next.js, FastAPI, PostgreSQL", "experimental",
            (Component("frontend", "node", ("next",)), Component("backend", "python", ("fastapi",)))),
    Adapter("java-spring", "Java Spring Boot (with an optional React + Vite front end)",
            "experimental", (Component("backend", "java", ("spring-boot",)), _REACT_VITE),
            note="needs the Java toolchain on the install"),
    Adapter("python-service", "Python service (FastAPI, Flask, Django, WSGI)", "best_effort",
            (Component("backend", "python", ("fastapi", "flask", "django", "wsgi")),)),
    Adapter("node-service", "Node service (Express, Fastify, Koa, Hono, NestJS, node:http)",
            "best_effort",
            (Component("backend", "node", ("express", "fastify", "koa", "hono", "nestjs",
                                           "node-http")),)),
    Adapter("node-web", "Node web front end (Vue, Svelte, Angular)", "best_effort",
            (Component("frontend", "node", ("vue", "svelte", "angular")),), databases=()),
    Adapter("java-dropwizard", "Java Dropwizard", "best_effort",
            (Component("backend", "java", ("dropwizard",)),),
            note="needs the Java toolchain on the install"),
    Adapter("go-service", "Go service (net/http, Gin, Echo, chi, Fiber)", "best_effort",
            (Component("backend", "go", ("net-http", "gin", "echo", "chi", "fiber")),),
            note="needs the Go toolchain on the install"),
)


def unsupported_adapters() -> list[dict[str, Any]]:
    """Every language BOSS refuses, as an adapter entry at level `unsupported`."""
    return [{"id": u.key, "label": u.label, "support_level": "unsupported",
             "languages": [u.key], "detection_files": sorted(
                 f for f, k in _UNSUPPORTED_MANIFESTS.items() if k == u.key)}
            for u in UNSUPPORTED]


def adapter(adapter_id: str) -> Optional[Adapter]:
    return next((a for a in ADAPTERS if a.id == adapter_id), None)


def language_support_level(key: str) -> str:
    """The best level of any adapter that builds `key` ("unsupported" for none)."""
    levels = [a.support_level for a in ADAPTERS if key in a.languages]
    return min(levels, key=SUPPORT_LEVELS.index) if levels else "unsupported"


def adapter_for(services: Iterable[Any]) -> Optional[Adapter]:
    """The adapter an app's detected services match (the most specific, best-level one):
    every service's language and framework is one of the adapter's components."""
    found = [(s.language if hasattr(s, "language") else s["language"],
              s.framework if hasattr(s, "framework") else s.get("framework"),
              s.role if hasattr(s, "role") else s.get("role"))
             for s in services]
    found = [f for f in found if f[2] in ("backend", "frontend")]
    if not found:
        return None
    best = None
    for a in ADAPTERS:
        ok = all(any(c.language == lang and c.role == role and (fw is None or fw in c.frameworks)
                     for c in a.components) for lang, fw, role in found)
        if ok and (best is None or SUPPORT_LEVELS.index(a.support_level)
                   < SUPPORT_LEVELS.index(best.support_level)):
            best = a
    return best


def adapter_for_template(stack: Optional[str], framework: Optional[str] = None,
                         layout: Optional["Layout"] = None) -> Optional[Adapter]:
    """The adapter a build is for, before any file exists: its layout, else its template."""
    if layout is not None:
        return adapter_for([
            {"language": layout.backend_language, "framework": layout.backend_framework,
             "role": "backend"},
            {"language": "node", "framework": layout.frontend_framework, "role": "frontend"}])
    if stack:
        return adapter_for([{"language": stack, "framework": framework, "role": "backend"}])
    return None


def certification_evidence(records: Iterable[Mapping[str, Any]], adapter_id: str) -> list[str]:
    """The run ids among benchmark records (services.benchmark) that meet PROMOTION_RULE for
    `adapter_id`. Promotion is a person's change to ADAPTERS; this says whether one may."""
    out = []
    for r in records:
        if (r.get("stack") == adapter_id and r.get("result") == "success"
                and isinstance(r.get("tests_total"), int) and r["tests_total"] > 0
                and r.get("tests_passed") == r["tests_total"]
                and r.get("deployment_smoke_test") == "pass"):
            out.append(str(r.get("run_id")))
    return out


def render_supported_stacks_md() -> str:
    """docs/community/SUPPORTED_STACKS.md, from the registry. Not per install (no "ready")."""
    lines = [
        "# Supported stacks",
        "",
        "<!-- Generated from engine/boss_engine/services/stacks.py (ADAPTERS, LANGUAGES). Do not "
        "edit by hand: run `python3 -m boss_engine.services.stacks --write-docs "
        "docs/community/SUPPORTED_STACKS.md` from engine/. A test fails when this file and the "
        "registry disagree. -->",
        "",
        "What BOSS builds, tests and starts, and how sure we are of it. Levels:",
        "",
        "- **certified** — the full workflow passed a recorded, repeatable benchmark run.",
        "- **experimental** — BOSS builds, tests and starts it; the full lifecycle is not yet "
        "certified.",
        "- **best effort** — BOSS attempts it with the same build code; no reliability claim.",
        "- **not supported** — refused before anything is spent (at Start, at architecture "
        "approval and at the build).",
        "",
        f"Promotion: {PROMOTION_RULE}",
        "",
        "| Stack | Level | Languages | Frameworks | Databases |",
        "|---|---|---|---|---|",
    ]
    for a in ADAPTERS:
        lines.append(f"| `{a.id}` — {a.label} | {LEVEL_WORDS[a.support_level]} | "
                     f"{', '.join(LANGUAGES[k].label for k in a.languages)} | "
                     f"{', '.join(a.frameworks)} | {', '.join(a.databases)} |")
    for u in unsupported_adapters():
        lines.append(f"| `{u['id']}` — {u['label']} | not supported | {u['label']} | — | — |")
    lines += ["", "## Languages", "",
              "| Language | Runtime | Package manager | Detected by | Toolchain |",
              "|---|---|---|---|---|"]
    for lang in LANGUAGES.values():
        lines.append(f"| {lang.label} | {lang.runtime} | {lang.package_manager} | "
                     f"{', '.join(f'`{m}`' for m in lang.manifests)} | "
                     f"{'always installed' if lang.toolchain is None else f'on by default (if turned off: `{toolchains.ENABLE_HINT.format(name=lang.toolchain)}`)'} |")
    lines += ["", "## Commands", "",
              "Each service runs its own, in its own directory. `—`: not a separate step.", "",
              "| Language | install | build | lint | typecheck | test | e2e |",
              "|---|---|---|---|---|---|---|"]
    for lang in LANGUAGES.values():
        cells = [f"`{lang.commands.get(k)}`" if lang.commands.get(k) else "—"
                 for k in ("install", "build", "lint", "typecheck", "test", "e2e")]
        lines.append(f"| {lang.label} | " + " | ".join(cells) + " |")
    lines += ["", "## Starting a backend and serving a front end", "",
              "BOSS starts the backend on `$PORT` (the command in the service's "
              "`boss.preview.command` wins) and asks `/` — or the path in `boss.preview.health` — "
              "for an answer before any e2e test runs. A front end whose Playwright config serves "
              "nothing is served built.", "",
              "| Framework | Role | Command |", "|---|---|---|"]
    for lang in LANGUAGES.values():
        for f in lang.frameworks:
            cmd = f.start or (_DEFAULT_START.get(lang.key, "") if f.role == "backend" else "")
            if cmd:
                lines.append(f"| {f.label} ({lang.label}) | {f.role} | `{cmd}` |")
    lines += ["", "Go builds a server with `go build` (the module root's `package main`, else "
              "the first `./cmd/<name>`) and runs it; it listens on `$PORT`.", ""]
    lines += ["A multi-service app is a backend and a web front end, each in its own "
              "directory with its own manifest. Data: `DATABASE_URL` (PostgreSQL) with a local "
              "SQLite file when it is unset, for local development.", ""]
    return "\n".join(lines)


def language_ready(key: str) -> bool:
    lang = LANGUAGES.get(key)
    return bool(lang) and (lang.toolchain is None or toolchains.has(lang.toolchain))


def enable_hint(key: str) -> Optional[str]:
    lang = LANGUAGES.get(key)
    if not lang or lang.toolchain is None or toolchains.has(lang.toolchain):
        return None
    return toolchains.ENABLE_HINT.format(name=lang.toolchain)


def capabilities() -> dict[str, Any]:
    """What this install can build, for the API, the console and every prompt that chooses a
    stack. Derived from LANGUAGES (the build functions) and toolchains.installed() (the image)."""
    have = toolchains.installed()
    languages = []
    for lang in LANGUAGES.values():
        ready = lang.toolchain is None or lang.toolchain in have
        languages.append({
            "key": lang.key, "label": lang.label, "ready": ready,
            "toolchain": lang.toolchain or "builtin",
            "versions": have.get(lang.toolchain, {}) if lang.toolchain else {},
            "enable": None if ready else toolchains.ENABLE_HINT.format(name=lang.toolchain),
            "backends": [f.label for f in lang.frameworks if f.role == "backend"],
            "frontends": [f.label for f in lang.frameworks if f.role == "frontend"],
            "frameworks": [f.key for f in lang.frameworks],
            "support_level": language_support_level(lang.key),
        })
    backends = [l["key"] for l in languages if l["backends"]]
    combos = [{"backend": b, "frontend": "node", "ready": all(
        x["ready"] for x in languages if x["key"] in (b, "node"))} for b in backends]
    return {"languages": languages, "multi_service": combos,
            "adapters": [a.as_dict() for a in ADAPTERS] + unsupported_adapters(),
            "promotion_rule": PROMOTION_RULE,
            "not_supported": [u.label for u in UNSUPPORTED]}


def supported_sentence(caps: Optional[dict[str, Any]] = None) -> str:
    """"Python (FastAPI, Flask, Django), Node (…) and Java (Spring Boot; off on this install …)"."""
    caps = caps or capabilities()
    parts = []
    for lang in caps["languages"]:
        names = lang["backends"] + ([f"{'/'.join(lang['frontends'][:3])} web front ends"]
                                    if lang["frontends"] else [])
        level = LEVEL_WORDS.get(lang.get("support_level") or "", "")
        part = (f"{lang['label']} ({', '.join(names)}; {level})" if names
                else f"{lang['label']} ({level})")
        if not lang["ready"]:
            part += f" — off on this install; turn it on with `{lang['enable']}`"
        parts.append(part)
    return ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]


def prompt_block() -> str:
    """The supported-stack list, for every prompt that picks a stack (panel, architect, build)."""
    caps = capabilities()
    ready = [l["label"] for l in caps["languages"] if l["ready"]]
    off = [f"{l['label']} (off on this install: `{l['enable']}`)" for l in caps["languages"]
           if not l["ready"]]
    return (
        "WHAT BOSS CAN BUILD ON THIS INSTALL (what is ready here, and what is off): "
        + supported_sentence(caps) + ". "
        "An app may be one service, or a backend in one of those languages plus a Node web "
        "front end, each in its own directory. "
        + (f"Ready now: {', '.join(ready)}. " if ready else "")
        + (f"Not turned on: {'; '.join(off)}. " if off else "")
        + "Not supported at all: " + ", ".join(caps["not_supported"])
        + " — never choose one of these; if the brief asks for one, say BOSS cannot build it. "
        "Keep the stack the brief asks for. A language that is not turned on is the blocking "
        "open question: enable its toolchain, accept another stack, or stop. Do not swap the "
        "brief's stack for a different one. "
        "The PRD must contain a line of the form `Stack: <service>=<language>/<framework>` "
        "for the stack the brief asks for.")


# ── Reading a stack out of words ────────────────────────────────────────────────────────────

def _sentence(text: str, start: int, end: int) -> tuple[str, str]:
    """(the sentence before the match, the sentence after it). Punctuation inside a code span
    never ends a sentence (the span is masked, same length, before splitting)."""
    masked = _CODE_SPAN.sub(lambda m: "`" + "_" * (len(m.group(0)) - 2) + "`", text)
    lo, hi = max(0, start - 200), end + 200
    before_m = masked[lo:start]
    cuts = list(_SENTENCE_END.finditer(before_m))
    cut_lo = lo + cuts[-1].end() if cuts else lo
    after_m = masked[end:hi]
    cut = _SENTENCE_END.search(after_m)
    cut_hi = end + cut.start() if cut else min(hi, len(text))
    return text[cut_lo:start], text[end:cut_hi]


def _other_language_words(key: Optional[str]) -> list[str]:
    words: list[str] = []
    for lang in LANGUAGES.values():
        if lang.key != key:
            words += [lang.label.lower(), *lang.words, *(w for f in lang.frameworks
                                                         for w in (f.label.lower(), *f.words))]
    for u in UNSUPPORTED:
        if u.key != key:
            words.append(u.label.lower())
    return [w for w in dict.fromkeys(words) if len(w) > 1]


def _is_source(text: str, start: int, end: int, key: Optional[str]) -> bool:
    """Is the language at text[start:end] what the sentence moves code AWAY from? Only in the
    explicit shapes above (_SOURCE_AFTER_VERB + a target language right after to/into/in,
    _SOURCE_AFTER_FROM, _REPLACED) — never because some other language appears later."""
    before, after = _sentence(text, start, end)
    if _SOURCE_AFTER_FROM.search(before) or _REPLACED.search(before):
        return True
    if not _SOURCE_AFTER_VERB.search(before):
        return False
    lead = _TARGET_LEAD.match(after)
    if lead is None:
        return False
    target = after[lead.end():].lower()
    return any(re.match(rf"{re.escape(w)}(?![\w])", target) for w in _other_language_words(key))


def _matches(pattern: str, text: str, key: Optional[str] = None, *,
             requirement: bool = False) -> bool:
    """Does `text` ask for what `pattern` names? Not under a negation ("not in Go", "has no
    node, java, go"), not as the SOURCE of a migration ("rewrite our Go codebase in Node"), and
    — for `requirement` patterns (a language's tooling) — only in a sentence that states one."""
    for m in re.finditer(pattern, text, re.I):
        before = text[max(0, m.start() - 40):m.start()]
        if _NEGATION.search(before) or _NEGATED_LIST.search(text[max(0, m.start() - 160):m.start()]):
            continue
        if _is_source(text, m.start(), m.end(), key):
            continue
        if requirement and not _REQUIREMENT.search("".join(_sentence(text, m.start(), m.end()))):
            continue
        return True
    return False


def _drop_inventory(m: "re.Match[str]") -> str:
    if _STACK_KEY.search(m.string[max(0, m.start() - 40):m.start()]):
        return m.group(0)
    return " "


def _requests_only(text: str) -> str:
    """Text without what names a language and asks for none: BOSS's own sentences about the
    deploy machine ("has no node, npm, pip3, java, go, ruby, php, make") and inventories of
    tool names (a spec's allowed-command list) — never an array its key calls the stack. The
    Northgate spec was read as asking for Ruby and PHP from exactly those (2026-11-15)."""
    return _NAME_INVENTORY.sub(_drop_inventory, _DEPLOY_BRIEFS.sub("", str(text or "")))


def unsupported_named(text: str) -> list[str]:
    """The unsupported languages a piece of text asks for (Rust, Ruby, …), in UNSUPPORTED order."""
    body = _requests_only(text)
    if not body.strip():
        return []
    return [u.key for u in UNSUPPORTED if any(_matches(p, body, u.key) for p in u.patterns)]


def languages_named(text: str) -> list[str]:
    """The supported languages a piece of text names (by language, framework or pattern)."""
    body = _requests_only(text)
    out = []
    for lang in LANGUAGES.values():
        words = list(lang.words) + [w for f in lang.frameworks for w in f.words]
        # "Java" is not "JavaScript".
        patterns = [r"\bjava\b(?!\s*script)" if w == "java" else rf"\b{re.escape(w)}\b"
                    for w in words] + list(lang.patterns)
        if any(_matches(p, body, lang.key) for p in patterns) or any(
                _matches(p, body, lang.key, requirement=True) for p in lang.tooling_patterns) \
                or lang.key in _stack_array_languages(body):
            out.append(lang.key)
    return out


def _stack_array_languages(text: str) -> set[str]:
    """Languages named as bare elements of an array its key calls the stack:
    `"stack": ["go", "react"]` names Go (a bare "go" is otherwise never the language)."""
    out: set[str] = set()
    for m in re.finditer(r"\[([^\[\]\n]{0,400})\]", text):
        if not _STACK_KEY.search(text[max(0, m.start() - 40):m.start()]):
            continue
        for item in re.findall(r"[\"']([^\"']{1,40})[\"']", m.group(1)):
            key = _normalise_language(item)
            if key in LANGUAGES:
                out.add(key)
    return out


def frontend_named(text: str) -> Optional[str]:
    body = str(text or "")
    for f in LANGUAGES["node"].frameworks:
        if f.role == "frontend" and any(_matches(rf"\b{re.escape(w)}\b", body) for w in f.words):
            return f.key
    return None


@dataclass
class StackVerdict:
    ok: bool
    unsupported: list[str] = field(default_factory=list)
    off: list[str] = field(default_factory=list)
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        caps = capabilities()
        return {"ok": self.ok, "code": None if self.ok else "stack_unsupported",
                "unsupported": [_label(k) for k in self.unsupported],
                "off": [{"language": _label(k), "enable": enable_hint(k)} for k in self.off],
                "message": self.message, "supported": caps}


def _label(key: str) -> str:
    if key in LANGUAGES:
        return LANGUAGES[key].label
    return next((u.label for u in UNSUPPORTED if u.key == key), key)


def verdict(unsupported: Iterable[str] = (), off: Iterable[str] = (), *,
            where: str = "This brief") -> StackVerdict:
    unsupported, off = list(dict.fromkeys(unsupported)), list(dict.fromkeys(off))
    if not unsupported and not off:
        return StackVerdict(True)
    sentences = []
    if unsupported:
        names = " or ".join(_label(k) for k in unsupported)
        sentences.append(f"{where} asks for {names}, which BOSS cannot build.")
    for key in off:
        sentences.append(f"{where} needs {_label(key)}, which is not turned on for this BOSS: "
                         f"turn it on with "
                         f"{toolchains.enable_instructions(LANGUAGES[key].toolchain or key)}, "
                         "then start again. BOSS does not build it in another language instead.")
    sentences.append(f"BOSS builds {supported_sentence()}.")
    if unsupported:
        sentences.append("Rewrite the brief for one of those, or start anyway to keep it as a "
                         "plan that is never built.")
    return StackVerdict(False, unsupported, off, " ".join(sentences))


def check_text(text: str, *, template_stack: Optional[str] = None,
               where: str = "This brief") -> StackVerdict:
    """Start: does the brief (and the template's stack) ask for something BOSS cannot build?"""
    unsupported = unsupported_named(text)
    named = languages_named(text)
    if template_stack:
        named.append(str(template_stack))
    off = [k for k in dict.fromkeys(named) if k in LANGUAGES and not language_ready(k)]
    unknown = [k for k in ([template_stack] if template_stack else []) if k not in LANGUAGES]
    return verdict(unsupported + unknown, off, where=where)


# -- The architecture's declared stack -------------------------------------------------------

#: The `Stack:` line, however the architect formatted it: a bullet, bold, or wrapped in backticks
#: exactly as STACK_LINE_INSTRUCTION shows it. A backticked line was not matched, so the check
#: fell back to prose and read "Rust/Ruby/PHP … unsupported" as a request (HelpDesk AI, D1 #3).
#: "Tech stack: Go" / "Tech Stack:" too (a ticket's wording).
_STACK_LINE = re.compile(r"^\s*(?:[-*]\s*)?[`*]*\s*(?:tech(?:nology)?\s+)?stack\s*[`*]*\s*[:—-]"
                         r"\s*[`*]*\s*(.+)$", re.I | re.M)
#: A fenced code block (``` or ~~~): a `Stack:` line inside one is sample text, not a declaration.
_CODE_FENCE = re.compile(r"^[ \t]*(```|~~~)[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)", re.S | re.M)


def _stack_line(markdown: str) -> "Optional[re.Match[str]]":
    """The first `Stack:` line outside fenced code blocks, or None."""
    return _STACK_LINE.search(_CODE_FENCE.sub("", str(markdown or "")))
#: Instructions for the architect: one line the approval check parses.
STACK_LINE_INSTRUCTION = (
    "Under 'Components', the FIRST line must declare the stack, in exactly this form: "
    "`Stack: backend=python/fastapi, frontend=node/react` (one `<service>=<language>/"
    "<framework>` per service; a single-service app has one entry). Use only the languages "
    "BOSS can build (listed above).")


def declared_stack(markdown: str) -> Optional[list[dict[str, str]]]:
    """[{service, language, framework}] from the architecture's `Stack:` line, or None."""
    m = _stack_line(markdown)
    if not m:
        return None
    out = []
    for part in re.split(r"[,;]", m.group(1).strip().strip("`*")):
        part = part.strip().strip("`* ")
        if not part:
            continue
        service, _, rest = part.partition("=") if "=" in part else ("app", "", part)
        language, _, framework = rest.strip().partition("/")
        out.append({"service": service.strip().lower(), "language": language.strip().lower(),
                    "framework": framework.strip().lower()})
    return out or None


_LANG_ALIASES = {"py": "python", "python3": "python", "nodejs": "node", "node.js": "node",
                 "javascript": "node", "typescript": "node", "js": "node", "ts": "node",
                 "jvm": "java", "golang": "go", "c#": "dotnet", ".net": "dotnet",
                 "csharp": "dotnet"}


def _normalise_language(word: str) -> str:
    word = word.strip().lower()
    word = _LANG_ALIASES.get(word, word)
    for lang in LANGUAGES.values():
        if word == lang.key or word in {f.key for f in lang.frameworks}:
            return lang.key
    return word


def _is_known_language(word: str) -> Optional[str]:
    cleaned = re.sub(r"^[^a-z0-9#+.]+|[^a-z0-9#+.]+$", "", str(word or "").strip().lower())
    if not cleaned:
        return None
    key = _normalise_language(cleaned)
    if key in LANGUAGES:
        return key
    for item in UNSUPPORTED:
        if key == item.key or key == item.label.lower():
            return item.key
    return None


def _language_set(value: Any, *, from_prose: bool) -> set[str]:
    """Languages a brief or a PRD names. A `Stack:` line wins. Prose is the brief only:
    a PRD with no line has not declared a stack, and that is not a swap."""
    if isinstance(value, str):
        declared = declared_stack(value)
        if declared:
            return {key for key in (_is_known_language(item["language"]) for item in declared)
                    if key}
        if not from_prose:
            return set()
        text = directive_text(value)
        named = languages_named(text) + unsupported_named(text)
        return {key for key in (_is_known_language(item) for item in named) if key}
    if isinstance(value, (list, tuple)):
        found: set[str] = set()
        for item in value:
            if isinstance(item, str):
                known = _is_known_language(item)
            elif isinstance(item, dict):
                known = _is_known_language(str(item.get("language") or ""))
            else:
                known = None
            if known:
                found.add(known)
        return found
    return set()


def image_languages() -> set[str]:
    """Languages the build image provides. Python and Node are always in it. Java and Go
    are in it only when the toolchain manifest names them — an empty manifest is an image
    that was not built with those stages, which is not the same as a stage waiting to be
    turned on."""
    have = toolchains.installed()
    return {lang.key for lang in LANGUAGES.values()
            if lang.toolchain is None or lang.toolchain in have}


def image_stack_block(*texts: Any, required_stack: Any = None) -> Optional[dict[str, Any]]:
    """A stack this build image cannot compile, or None when every asked language is in it.

    Asked means a `Stack:` line (including "Stack: Go", which is not a bare word "go"),
    a language the prose names, a required_stack entry, or a stack adapter id such as
    go-service. A language the image does not carry is refused here, before a project or
    a run exists. The fix is to add it to the build image or to choose Node. The start
    door's "boss toolchains enable" sentence is a different case and is not this one.
    Rust and the other unsupported languages are left for that door."""
    asked: set[str] = set()
    for text in texts:
        raw = str(text or "").strip()
        if not raw:
            continue
        if " " not in raw and "\n" not in raw and len(raw) <= 40:
            lone = _is_known_language(raw)
            if lone in LANGUAGES:
                asked.add(lone)
        declared = declared_stack(raw)
        if declared:
            for item in declared:
                key = _is_known_language(item.get("language") or "")
                if key in LANGUAGES:
                    asked.add(key)
                    continue
                found = adapter(str(item.get("language") or ""))
                if found:
                    asked.update(lang for lang in found.languages if lang in LANGUAGES)
        asked.update(key for key in languages_named(raw) if key in LANGUAGES)
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,80}", raw):
            found = adapter(token)
            if found:
                asked.update(lang for lang in found.languages if lang in LANGUAGES)
    if required_stack is not None:
        asked.update(key for key in _language_set(required_stack, from_prose=False)
                     if key in LANGUAGES)
    provided = image_languages()
    missing = [lang.key for lang in LANGUAGES.values()
               if lang.key in asked and lang.key not in provided]
    if not missing:
        return None
    label = LANGUAGES[missing[0]].label
    return {
        "error": "stack_not_in_image",
        "language": missing[0],
        "provided": sorted(provided),
        "message": (f"This build image does not provide {label}. "
                    f"Fix: add {label} to the build image, or choose Node."),
    }


def check_deviation(required: Any, declared: Any) -> Optional[dict[str, list[str]]]:
    """How `declared` (a PRD, or its stack) differs from `required` (the brief, or its stack).

    None when either side names nothing, or they name the same languages. A Node PRD for a
    Go brief is a deviation: Go is missing and Node was substituted. Empty declared is not
    a swap — the PRD has not named a stack yet."""
    asked = _language_set(required, from_prose=True)
    got = _language_set(declared, from_prose=False)
    if not asked or not got or asked == got:
        return None
    return {
        "required": sorted(asked),
        "declared": sorted(got),
        "missing": sorted(asked - got),
        "substituted": sorted(got - asked),
    }


_STACK_CARD_OPTIONS = (
    {"key": "enable_toolchain", "action": "enable_toolchain", "label": "Enable toolchain"},
    {"key": "accept_alt", "action": "accept_alt", "label": "Accept alt"},
    {"key": "stop", "action": "stop", "label": "Stop"},
)


def stack_card(required: Any, declared: Any) -> Optional[dict[str, Any]]:
    """The blocking question when a PRD's stack is not the brief's, or None when it is."""
    deviation = check_deviation(required, declared)
    if not deviation:
        return None
    asked = ", ".join(deviation["required"])
    got = ", ".join(deviation["declared"])
    return {
        "question": (
            f"The brief requires {asked}. The PRD declares {got}. "
            "The toolchain is the blocking open question: enable it, accept this stack, or stop."
        ),
        "options": [dict(option) for option in _STACK_CARD_OPTIONS],
        "deviation": deviation,
    }


def stack_card_for_context(context: Any) -> Optional[dict[str, Any]]:
    """The stack card for a workflow run: the brief (or its required_stack) against the PRD."""
    if not isinstance(context, dict):
        return None
    run_input = context.get("input") if isinstance(context.get("input"), dict) else {}
    required = run_input.get("required_stack") or run_input.get("directive") or ""
    prd = ""
    steps = context.get("steps") if isinstance(context.get("steps"), dict) else {}
    for produced in steps.values():
        if not isinstance(produced, dict):
            continue
        if produced.get("prd_markdown"):
            prd = str(produced["prd_markdown"])
            break
        meeting = produced.get("meeting")
        if isinstance(meeting, dict) and meeting.get("prd_markdown"):
            prd = str(meeting["prd_markdown"])
            break
    return stack_card(required, prd)


def ensure_stack_line(markdown: str, required: Any) -> str:
    """The PRD's `Stack:` line. An existing line is kept, including one that swaps the stack.
    A missing line is filled from the brief so `declared_stack` can read what was asked."""
    text = str(markdown or "")
    if declared_stack(text):
        return text
    if isinstance(required, (list, tuple)):
        parts = []
        for item in required:
            if isinstance(item, dict):
                lang = _is_known_language(str(item.get("language") or ""))
                component = re.sub(r"[^a-z0-9._-]", "",
                                    str(item.get("component") or "app").strip().lower()) or "app"
            else:
                lang = _is_known_language(str(item))
                component = "app"
            if lang:
                parts.append(f"{component}={lang}")
    else:
        parts = [f"app={lang}" for lang in sorted(_language_set(required, from_prose=True))]
    if not parts:
        return text
    line = "Stack: " + ", ".join(parts)
    if text and not text.endswith("\n"):
        text += "\n"
    return f"{text}{line}\n"


def check_architecture(markdown: str) -> StackVerdict:
    """Architecture approval: the stack the architecture DECLARES (its `Stack:` line), else the
    stack its components and deployment sections name — never the alternatives it rejected."""
    declared = declared_stack(markdown)
    if declared:
        langs = [_normalise_language(d["language"]) for d in declared]
        # Only a language BOSS can NAME is a verdict. A `Stack:` line written as prose ("one npm
        # project in helpdesk-ai/ …") is not a language called "one npm project": refusing it
        # stopped a Node build that BOSS builds. Words it cannot place are read as text below.
        unknown = [k for k in langs if k not in LANGUAGES
                   and not any(u.key == k or u.label.lower() == k for u in UNSUPPORTED)]
        if not unknown:
            unsupported = [next(u.key for u in UNSUPPORTED if u.key == k or u.label.lower() == k)
                           for k in langs if k not in LANGUAGES]
            off = [k for k in langs if k in LANGUAGES and not language_ready(k)]
            return verdict(unsupported, off, where="The architecture")
        line = _stack_line(markdown)
        named = check_text(line.group(1) if line else "", where="The architecture")
        if not named.ok or languages_named(line.group(1) if line else ""):
            return named
    from .architecture_docs import parse_sections

    sections = parse_sections(markdown or "")
    body = "\n".join(sections.get(k, "") for k in ("context", "components", "deployment",
                                                    "data_model", "apis")) or str(markdown or "")
    return check_text(body, where="The architecture")


_TEMPLATE_MATERIAL = re.compile(
    r"(?:<<<BEGIN UNTRUSTED TEMPLATE MATERIAL>>>|^## The template's brief).*?(?=\n-{3,}\s*\n|\Z)",
    re.S | re.M)
_ARCHITECTURE_PROSE = re.compile(r"THE APPROVED ARCHITECTURE BELOW IS BINDING.*\Z", re.S)
#: BOSS's own deploy briefs, added to a build's directive (workflow_capability_runner
#: DEPLOY_HOST_MARKER / DEPLOY_BRIEF_MARKER): a paragraph each, up to the next blank line. They
#: describe the MACHINE ("has no node, npm, pip3, java, go, ruby, php, make") — never a request.
_DEPLOY_BRIEFS = re.compile(
    r"^(?:THE MACHINE THIS APP DEPLOYS TO|THIS APP WILL BE DEPLOYED)\b.*?(?=\n\s*\n|\Z)",
    re.S | re.M)


def operator_text(instructions: str) -> str:
    """A build brief without the material BOSS added that is not a request: an applied template's
    brief and reference code (it describes every stack it ships, Java and React included), BOSS's
    briefs about the deploy machine, and the approved architecture's prose (whose stack is read
    from its `Stack:` line instead)."""
    text = _TEMPLATE_MATERIAL.sub("", str(instructions or ""))
    # HelpDesk AI's "add a feature" build (crun_d3af188d7da7402d930d) was refused as asking for
    # Java, Ruby and PHP: the only place those words appeared was BOSS's own sentence about the
    # deploy machine, "has no node, npm, pip3, java, go, ruby, php, make".
    text = _DEPLOY_BRIEFS.sub("", text)
    return _ARCHITECTURE_PROSE.sub("", text)


def directive_text(instructions: str) -> str:
    """The operator's request in a build brief: operator_text up to the first `---`, where the
    product spec (or the approved architecture) begins. What the spec weighs is not asked for."""
    return re.split(r"\n-{3,}\s*\n", operator_text(instructions), maxsplit=1)[0]


def check_build(instructions: str, *, template_stack: Optional[str] = None) -> StackVerdict:
    """The build's last guard before anything is spent: the approved architecture's declared
    stack when the brief carries one, else the directive (the text before the spec) and the
    template's stack. The spec and the architecture's prose are not read for language names:
    an alternative the architect rejected is not a request.

    THE STACK THE OPERATOR ASKED FOR IS NEVER SWAPPED. Northgate's brief asked for a Go gateway
    on an install without Go; the panel "converged" on Node and the build shipped Node
    (2026-11-15). A language the directive asks for that is off on this install, or that BOSS
    cannot build, stops the build — with how to turn it on — even when an architecture declares
    another stack: proceeding in a different language is never the answer to a missing one."""
    text = str(instructions or "")
    asked = check_text(directive_text(text), template_stack=template_stack, where="This build")
    declared = declared_stack(text)
    if declared:
        result = check_architecture(text)
        if template_stack and template_stack in LANGUAGES and not language_ready(template_stack):
            result = verdict(result.unsupported, [*result.off, template_stack], where="This build")
        if not asked.ok:
            return verdict([*asked.unsupported, *result.unsupported], [*asked.off, *result.off],
                           where="This build")
        if not result.ok and result.message.startswith("This build"):
            return result
        return StackVerdict(result.ok, result.unsupported, result.off,
                            result.message.replace("The architecture", "This build's approved "
                                                   "architecture", 1))
    return asked


# ── The services an app directory holds ─────────────────────────────────────────────────────

def _service_in(path: Path, rel: str) -> Optional[Service]:
    """The service `path` is, or None. A directory BOSS's uid may not look inside is not a
    service BOSS can build, never a crash."""
    try:
        return _service_in_unsafe(path, rel)
    except OSError:
        return None


def _service_in_unsafe(path: Path, rel: str) -> Optional[Service]:
    """The service `path` is (by its manifests), or None when it holds none."""
    for manifest, key in _UNSUPPORTED_MANIFESTS.items():
        if (path / manifest).is_file():
            return Service(rel, key, None, "backend", supported=False)
    if compiled_stack(path) == "go":
        text = _read(path / "go.mod")
        framework = next((f.key for f in LANGUAGES["go"].frameworks
                          if any(m in text for m in f.markers)), "net-http")
        return Service(rel, "go", framework, "backend")
    if any((path / m).is_file() for m in JAVA_MANIFESTS):
        text = _read(path / "pom.xml") + _read(path / "build.gradle") + _read(path / "build.gradle.kts")
        framework = next((f.key for f in LANGUAGES["java"].frameworks
                          if any(m in text for m in f.markers)), None)
        return Service(rel, "java", framework, "backend")
    if (path / "package.json").is_file():
        pkg = _package(path)
        deps = _deps(pkg)
        node = LANGUAGES["node"]
        back = next((f.key for f in node.frameworks if f.role == "backend"
                     and any(m in deps for m in f.markers)), None)
        front = next((f.key for f in node.frameworks if f.role == "frontend"
                      and any(m in deps for m in f.markers)), None)
        if back:
            return Service(rel, "node", back, "backend")
        if front:
            return Service(rel, "node", front, "frontend")
        if "@playwright/test" in deps or "playwright" in deps:
            return Service(rel, "node", None, "e2e")
        if "start" in _scripts(pkg) or (path / "server.js").is_file():
            return Service(rel, "node", None, "backend")
        return Service(rel, "node", None, "unknown")
    if any((path / m).is_file() for m in PYTHON_MANIFESTS) or (path / "manage.py").is_file():
        text = (_read(path / "requirements.txt") + _read(path / "pyproject.toml")
                + _read(path / "Pipfile") + _read(path / "setup.py")).lower()
        framework = next((f.key for f in LANGUAGES["python"].frameworks
                          if any(re.search(rf"(?m)^\s*[\"']?{re.escape(m)}\b", text)
                                 or f'"{m}' in text for m in f.markers)), None)
        if (path / "manage.py").is_file():
            framework = "django"
        return Service(rel, "python", framework, "backend")
    return None


def _count_sources(base: Path, suffix: str, cap: int = 400) -> int:
    n = 0
    stack = [(base, 0)]
    while stack and n < cap:
        d, level = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_symlink():
                continue
            if p.is_dir():
                if level < 4 and p.name not in SKIP_DIRS - {"src"} and not p.name.startswith("."):
                    stack.append((p, level + 1))
            elif p.suffix == suffix:
                n += 1
    return n


def compiled_stack(path: Path | str) -> Optional[str]:
    """"java" or "go" when `path` is a build of either (pom.xml / build.gradle, go.mod), else None.
    A directory with BOTH is the one most of its source is in, Java on a tie: a stray go.mod in
    a Maven project does not make it a Go service."""
    base = Path(path)
    try:
        java = any((base / m).is_file() for m in JAVA_MANIFESTS)
        go = any((base / m).is_file() for m in GO_MANIFESTS)
    except OSError:
        return None
    if java and go:
        return "go" if _count_sources(base, ".go") > _count_sources(base, ".java") else "java"
    return "java" if java else "go" if go else None


def unsupported_manifest(path: Path | str) -> Optional[tuple[str, str]]:
    """(language label, manifest) when `path` holds the manifest of a language BOSS cannot
    build (Cargo.toml, Gemfile, …), else None."""
    for manifest, key in _UNSUPPORTED_MANIFESTS.items():
        try:
            if (Path(path) / manifest).is_file():
                return _label(key), manifest
        except OSError:
            continue
    return None


def _children(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.iterdir() if p.is_dir() and not p.is_symlink()
                      and p.name not in SKIP_DIRS and not p.name.startswith("."))
    except OSError:
        return []


def detect_services(app_dir: Path | str) -> list[Service]:
    """Every service in an app directory: the directory itself, its immediate subdirectories,
    and one level below apps/ services/ packages/."""
    base = Path(app_dir)
    found: list[Service] = []
    own = _service_in(base, ".")
    if own is not None:
        found.append(own)
    for child in _children(base):
        dirs = [child]
        if child.name in GROUP_DIRS:
            dirs = _children(child)
        for d in dirs:
            svc = _service_in(d, d.relative_to(base).as_posix())
            if svc is not None:
                found.append(svc)
    return found


def is_multi_service(services: list[Service]) -> bool:
    """More than one service, in different directories, of which one serves (a backend) and
    another is a web front end or a second backend. An e2e harness alone is not a service."""
    real = [s for s in services if s.role in ("backend", "frontend")]
    if len(real) < 2 or len(services) > MAX_SERVICES:
        return False
    return any(s.role == "backend" for s in real)


def find_stack_root(root: Path | str, changed: Optional[list[str]] = None,
                    app_id: Optional[str] = None) -> Optional[Path]:
    """The multi-service app directory this run builds, or None. The DEEPEST directory on a
    changed path that is multi-service (a monorepo directory of many apps never is: more than
    MAX_SERVICES), then the named app, then the repository root."""
    base = Path(root)
    tried: set[Path] = set()

    def ok(d: Path) -> bool:
        if d in tried or not d.is_dir():
            return False
        tried.add(d)
        return is_multi_service(detect_services(d))

    for rel in changed or []:
        parts = PurePosixPath(str(rel)).parts
        for depth in range(len(parts) - 1, 0, -1):
            d = base.joinpath(*parts[:depth])
            if ok(d):
                return d
    aid = str(app_id or "").strip().strip("/")
    if aid and ok(base / aid):
        return base / aid
    # THE REPOSITORY ROOT ONLY FOR WORK THAT LIVES THERE. A build whose changes all sit inside
    # one app directory (or that names its app) is that app, even when it is single-service:
    # the root of a shared repository holds OTHER apps. HelpDesk (helpdesk-ai/, one npm
    # project) was refused "needs Java" because paramg/boss-sandbox's root has a pom.xml.
    # The root is the app only when the build changed the root's OWN manifest. Changes in
    # .github/ (its CI file), docs/ or one app folder never make the shared root the app:
    # HelpDesk's .github/workflows/helpdesk-ci.yml defeated a "one top folder" rule.
    root_manifests = {"package.json", "pom.xml", "build.gradle", "build.gradle.kts",
                      "requirements.txt", "pyproject.toml", "setup.py", "go.mod", "Cargo.toml"}
    root_changed = any(len(PurePosixPath(str(rel)).parts) == 1
                       and PurePosixPath(str(rel)).name in root_manifests
                       for rel in (changed or []))
    # And never on no evidence: an empty change list (a repair seeded from a saved workspace
    # whose own diff is empty, crun_03e44800) fell through to the root and 'needed Java' again.
    # A root laid out as a full-stack app (backend/ + frontend/ under it) is the app when the
    # change touches TWO OR MORE of its services; a change inside one app folder never is.
    if not root_changed and not (aid and (base / aid).is_dir()):
        try:
            service_dirs = {svc.rel.split("/", 1)[0] for svc in detect_services(base)
                            if svc.rel not in ("", ".")}
        except OSError:
            service_dirs = set()
        touched = {PurePosixPath(str(r)).parts[0] for r in (changed or [])
                   if len(PurePosixPath(str(r)).parts) > 1}
        if len(touched & service_dirs) >= 2:
            return base if ok(base) else None
        return None
    if aid and (base / aid).is_dir():
        return None
    return base if ok(base) else None


# ── The plan ───────────────────────────────────────────────────────────────────────────────

@dataclass
class StackPlan:
    app_dir: Path
    services: list[Service]
    #: (command, absolute dir): each service's install, build and unit tests, backends first.
    steps: list[tuple[str, Path]] = field(default_factory=list)
    backend: Optional[Service] = None
    backend_command: Optional[list[str]] = None
    backend_health: str = "/"
    frontend: Optional[Service] = None
    #: (command, absolute dir) run with the backend up and BACKEND_ENV set.
    e2e_steps: list[tuple[str, Path]] = field(default_factory=list)
    #: The frontend needs BOSS to serve it (its Playwright config starts no webServer), and
    #: the command that serves it built on $PORT (`vite preview`, `next start`).
    serve_frontend: bool = False
    frontend_command: Optional[list[str]] = None
    error: Optional[str] = None

    def dir_of(self, service: Service) -> Path:
        return self.app_dir if service.rel == "." else self.app_dir / service.rel

    def service_for(self, language: str) -> Optional[Service]:
        """The service a template of `language` lives in (its backend first)."""
        matches = [s for s in self.services if s.language == language]
        return next((s for s in matches if s.role == "backend"), matches[0] if matches else None)

    def describe(self, root: Optional[Path] = None) -> str:
        def where(p: Path) -> str:
            try:
                return p.relative_to(root).as_posix() if root else str(p)
            except ValueError:
                return str(p)
        parts = [f"({where(d)}) {c}" for c, d in self.steps]
        if self.backend is not None and self.backend_command:
            parts.append(f"start backend ({where(self.dir_of(self.backend))}): "
                         f"{self.backend_command[-1]}")
        parts += [f"({where(d)}) {c}" for c, d in self.e2e_steps]
        return " ; ".join(parts)

    def as_dict(self, root: Optional[Path] = None) -> dict[str, Any]:
        return {"app_dir": str(self.app_dir.relative_to(root)) if root and
                self.app_dir != root else ".",
                "services": [s.as_dict() for s in self.services],
                "backend_command": (self.backend_command or [None])[-1],
                "description": self.describe(root), "error": self.error}


def _playwright_has_webserver(path: Path) -> bool:
    for name in ("playwright.config.ts", "playwright.config.js", "playwright.config.mjs"):
        if "webServer" in _read(path / name):
            return True
    return False


def stack_plan(app_dir: Path | str, services: Optional[list[Service]] = None) -> StackPlan:
    """Per-service install/build/unit tests, the backend's start command, and the e2e commands
    that run against it."""
    from .verification_target import PLAYWRIGHT_INSTALL_CMD

    base = Path(app_dir)
    services = services if services is not None else detect_services(base)
    plan = StackPlan(base, services)
    bad = [s for s in services if not s.supported]
    if bad:
        plan.error = verdict([s.language for s in bad], where=f"This app ({', '.join(s.rel for s in bad)})"
                             ).message
        return plan
    off = [s.language for s in services if not language_ready(s.language)]
    if off:
        plan.error = verdict((), off, where="This app").message
        return plan
    order = {"backend": 0, "frontend": 1, "unknown": 2, "e2e": 3}
    for svc in sorted(services, key=lambda s: (order.get(s.role, 9), s.rel)):
        plan.steps += LANGUAGES[svc.language].steps(svc, plan.dir_of(svc))
    plan.backend = next((s for s in services if s.role == "backend"), None)
    if plan.backend is not None:
        path = plan.dir_of(plan.backend)
        from .preview_service import _declared_command_in, preview_health_path

        try:
            declared = _declared_command_in(path)
        except ValueError as error:      # a malformed boss.preview.report: say how to fix it
            declared, plan.error = None, str(error)
        plan.backend_command = (["sh", "-lc", declared] if declared
                                else LANGUAGES[plan.backend.language].start(plan.backend, path))
        plan.backend_health = preview_health_path(path)
        if not plan.backend_command:
            plan.error = (f"BOSS cannot tell how to start the {LANGUAGES[plan.backend.language].label} "
                          f"backend in {plan.backend.rel}/: write the command that starts it (on "
                          f"$PORT) in {plan.backend.rel}/boss.preview.command.")
    plan.frontend = next((s for s in services if s.role == "frontend"), None)
    for svc in [s for s in services if s.role in ("frontend", "e2e")]:
        path = plan.dir_of(svc)
        scripts = _scripts(_package(path))
        wanted = [n for n in NODE_E2E_SCRIPTS if scripts.get(n, "").strip()]
        if "test:e2e" in wanted:
            plan.e2e_steps.append((PLAYWRIGHT_INSTALL_CMD, path))
        plan.e2e_steps += [(f"npm run {n}", path) for n in wanted]
        if svc is plan.frontend and "test:e2e" in wanted and not _playwright_has_webserver(path):
            plan.serve_frontend = True
            fw = next((f for f in LANGUAGES["node"].frameworks if f.key == svc.framework), None)
            plan.frontend_command = ["sh", "-lc", (fw.start if fw and fw.start else VITE_SERVE)]
    return plan


# ── What the build is MEANT to be, before any file exists ──────────────────────────────────

@dataclass
class Layout:
    """The services a full-stack build is asked for, for the test author's and coder's prompts
    and for the gate before the tree shows them."""
    backend_language: str
    backend_framework: Optional[str]
    frontend_framework: str
    backend_dir: str = "backend"
    frontend_dir: str = "frontend"

    def as_dict(self) -> dict[str, Any]:
        return {"backend": f"{self.backend_language}/{self.backend_framework or ''}".rstrip("/"),
                "frontend": f"node/{self.frontend_framework}",
                "dirs": [self.backend_dir, self.frontend_dir]}


def intended_layout(text: str, *, template_stack: Optional[str] = None,
                    template_framework: Optional[str] = None,
                    declared: Optional[list[dict[str, str]]] = None) -> Optional[Layout]:
    """A backend in a NON-Node language plus a Node web front end, asked for in words (or by the
    architecture's Stack line, or a Python/Java template plus a named front end). None for
    everything else — a single-service app, and a Node app with a UI, keep today's behaviour
    (the tree decides those: services that land in separate directories are planned per
    service whatever the brief said)."""
    body = str(text or "")
    declared = declared if declared is not None else declared_stack(body)
    backend_lang, backend_fw, front = None, None, None
    if declared and len(declared) >= 2:
        for d in declared:
            lang = _normalise_language(d["language"])
            fw = d["framework"] or None
            role = next((f.role for f in LANGUAGES.get(lang, LANGUAGES["node"]).frameworks
                         if f.key == fw), None) if lang in LANGUAGES else None
            if lang == "node" and (role == "frontend" or d["service"] in ("frontend", "web", "ui", "client")):
                front = fw or "react"
            elif lang in LANGUAGES and backend_lang is None:
                backend_lang, backend_fw = lang, fw
    else:
        front = frontend_named(body)
        named = [k for k in languages_named(body) if k != "node"]
        backend_lang = template_stack if template_stack in LANGUAGES else (named[0] if named else None)
        if backend_lang:
            backend_fw = template_framework or next(
                (f.key for f in LANGUAGES[backend_lang].frameworks if f.role == "backend"
                 and any(_matches(rf"\b{re.escape(w)}\b", body) for w in f.words)), None)
    if not backend_lang or backend_lang == "node" or not front:
        return None
    dirs = re.findall(r"\b(backend|server|api|frontend|client|web|ui)/", body)
    back_dir = next((d for d in dirs if d in ("backend", "server", "api")), "backend")
    front_dir = next((d for d in dirs if d in ("frontend", "client", "web", "ui")), "frontend")
    return Layout(backend_lang, backend_fw, front, back_dir, front_dir)


def layout_prompt(layout: Layout, app_root: Optional[str], *, template_command: Optional[str] = None,
                  template_stack: Optional[str] = None) -> str:
    """How a full-stack app is laid out and how BOSS builds and tests it — for the test author,
    the coder and the solo builder, so a JS harness never lands at the root of a Python app."""
    root = (app_root or "<the app directory>").strip("/")
    back = LANGUAGES[layout.backend_language]
    bfw = next((f.label for f in back.frameworks if f.key == layout.backend_framework), back.label)
    ffw = next((f.label for f in LANGUAGES["node"].frameworks if f.key == layout.frontend_framework),
               layout.frontend_framework)
    b, f = f"{root}/{layout.backend_dir}", f"{root}/{layout.frontend_dir}"
    if back.key == "python":
        back_rules = (f"its own requirements.txt (with {bfw.lower()} and its server, e.g. "
                      "uvicorn); unit and API tests are pytest files under "
                      f"{b}/tests/ (BOSS runs `{PYTEST}` there)")
        start_example = "python3 -m uvicorn main:app --host 0.0.0.0 --port $PORT"
    elif back.key == "java":
        back_rules = ("its own pom.xml or build.gradle; unit tests under src/test (BOSS runs "
                      f"`{toolchains.MAVEN_GATE[0]}` or `{toolchains.GRADLE_GATE[0]}` there)")
        start_example = "java -Dserver.port=$PORT -jar target/app.jar"
    else:
        back_rules = "its own package.json with a `start` script and its own tests"
        start_example = "node server.js"
    if layout.frontend_framework == "next":
        front_rules = (
            f"- {f}/ — the Node (Next.js) web front end: its own package.json with the scripts "
            "build (`next build`), start (`next start`), typecheck, lint and test:e2e, and "
            "@playwright/test in devDependencies. Its Playwright specs live in "
            f"{f}/e2e/. next.config rewrites the API paths (e.g. /api, /auth) to "
            "process.env.BACKEND_URL (default http://127.0.0.1:8000), and playwright.config's "
            "webServer serves the built app (`npm run start`).\n")
    else:
        front_rules = (
            f"- {f}/ — the Node ({ffw}) web front end: its own package.json with the scripts "
            "build, typecheck, lint and test:e2e, and @playwright/test and vite in "
            f"devDependencies. Its Playwright specs live in {f}/e2e/. Its vite config proxies "
            "the API paths (e.g. /api, /auth) to process.env.BACKEND_URL (default "
            "http://127.0.0.1:8000), and playwright.config's webServer serves the built front "
            "end (`npm run preview`).\n")
    tpl = ""
    if template_command and template_stack:
        where = b if template_stack == back.key else f
        tpl = (f"\n- The template's own tests go in {where}/ at the relative paths the template "
               f"gives, and BOSS runs them there with `{template_command}` while the backend is up.")
    return (
        f"\n\nTHIS APP IS TWO SERVICES IN ONE APP DIRECTORY ({root}/). Lay it out exactly so:\n"
        f"- {b}/ — the {back.label} ({bfw}) backend: {back_rules}. It reads PORT from the "
        f"environment and declares how it starts in {b}/boss.preview.command (one line, e.g. "
        f"`{start_example}`).\n"
        + front_rules
        + "- Data: read DATABASE_URL (PostgreSQL; BOSS provides one to the build when the app "
        "needs it) and fall back to a local SQLite file when it is unset, for local "
        "development.\n"
        f"- Nothing else at {root}/ itself: no package.json, no Playwright config, no tests "
        "there. Never write JS tests in the backend or Python tests in the front end.\n"
        "HOW BOSS BUILDS AND TESTS IT: each service's own install, build and unit tests, in its "
        f"own toolchain; then BOSS STARTS the backend with {layout.backend_dir}/"
        "boss.preview.command, sets BACKEND_URL (and BOSS_BACKEND_URL) to it, and runs "
        f"`npm run test:e2e` in {f}/ against it. Browser tests that need the API go through the "
        "front end's proxy to the started backend, never a mock of it." + tpl + "\n")


def build_layout(instructions: str, title: str = "", *, template_stack: Optional[str] = None,
                 template_framework: Optional[str] = None) -> Optional[Layout]:
    """intended_layout for a build brief: the architecture's Stack line wherever it is, else the
    operator's own words — the directive, as check_build reads it — never the template's, the
    architecture's or the product spec's prose.

    THE SPEC IS NOT A REQUEST FOR A STACK. HelpDesk AI's "add SLA timers" PRD told the builder to
    "confirm whether the backend is Python (FastAPI/Flask/Django) or Node (… Next.js)"; read for
    language names, that became "a Python backend in backend/ and a Next front end in frontend/"
    and failed an existing Node app whose 133 tests passed (crun_db8299feecc449fcbe60)."""
    return intended_layout(f"{title}\n{directive_text(instructions)}",
                           template_stack=template_stack, template_framework=template_framework,
                           declared=declared_stack(instructions))


# ── Start: refuse before anything is spent ──────────────────────────────────────────────────

#: The field that starts a run anyway (the brief is kept as a plan nobody builds).
OVERRIDE_FIELD = "allow_unsupported_stack"


class StackUnsupported(Exception):
    """A start refused with code `stack_unsupported`. `verdict.as_dict()` is the API's detail."""

    code = "stack_unsupported"

    def __init__(self, verdict_: StackVerdict) -> None:
        super().__init__(verdict_.message)
        self.verdict = verdict_

    def detail(self) -> dict[str, Any]:
        return {"error": self.code, **self.verdict.as_dict(),
                "override": f"send {OVERRIDE_FIELD}: true to start it anyway"}


def _workflow_builds(connection: Any, version_id: Optional[str]) -> bool:
    """Does the workflow version have a build step? Unknown (no version) counts as yes."""
    if not version_id:
        return True
    from sqlalchemy import text as _text

    from ..templates.apply import _has_build_step

    spec = connection.execute(_text("SELECT spec FROM workflow_versions WHERE id = :v"),
                              {"v": version_id}).scalar()
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            return True
    return spec is None or _has_build_step(spec)


def refuse_unsupported_start(connection: Any, *, version_id: Optional[str],
                             run_input: Mapping[str, Any], allow: bool = False) -> None:
    """Start (POST /v1/delivery-workflows/start, the agent's start_workflow): raise
    StackUnsupported when a workflow that BUILDS is started on a brief asking for a stack BOSS
    cannot build here. A workflow with no build step (research, a decision) is never refused:
    it can discuss Go all it likes."""
    if allow or not isinstance(run_input, Mapping):
        return
    text = "\n".join(str(run_input.get(k) or "") for k in ("title", "directive", "brief",
                                                             "constraints"))
    tpl = run_input.get("template") if isinstance(run_input.get("template"), Mapping) else {}
    t_stack = str((tpl or {}).get("stack") or "") or None
    result = check_text(operator_text(text), template_stack=t_stack)
    if result.ok or not _workflow_builds(connection, version_id):
        return
    raise StackUnsupported(result)


if __name__ == "__main__":  # python3 -m boss_engine.services.stacks --write-docs <path>
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "--write-docs":
        Path(sys.argv[2]).write_text(render_supported_stacks_md(), encoding="utf-8")
    else:
        print(render_supported_stacks_md())
