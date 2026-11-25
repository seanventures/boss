"""An API has no page at "/": the preview says so, and links the routes that answer.

THE INCIDENT (prev_82bfd120d83a4662a23a, northgate-gateway, 2026-11-21). The preview ran fine —
`CONFIG_DIR=config go run ./cmd/northgate-gateway` on its port, /healthz and /readyz answering
200 — but "Open preview" opens the app's root, a gateway routes only what its config names, and
the browser showed the gateway's bare 404 "unmatched". Every person who pressed the button read
"the preview is broken".

Now, when a browser LOADS the root of a running preview and the app answers it with a "nothing
here" 4xx, the preview proxy serves a small BOSS page instead: "This app is an API — there is no
page at /", the app's own answer, and the routes that DO answer, each a link. Those routes come
from what BOSS already knows — the preview's declared health/ready path, the project's declared
health checks, the app's OpenAPI document and the routes its source and config register — and
each SAFE one is asked through the same proxy before it is listed, so only a route that
answers is a link. Only routes known to be reads are ever asked — declared health/ready paths,
target checks, OpenAPI GET routes and the conventional health list. A route merely found in the
code or config (HandleFunc("/logout") names no method) is shown as text and never requested.

Only a preview BOSS has a running row for gets the page: the gateway's own "preview not found"
404 must never be mistaken for an app's answer.

Generic by construction: nothing here knows any particular app. A script's fetch of "/" (an API
client, a health checker) still gets the app's own 404; only a page load is answered.
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

#: What an app says at "/" when it has nothing there. Not 401/403/407: an app asking a person to
#: sign in at its root HAS a page there, and its challenge must reach the browser.
ROOT_MISS_STATUSES = frozenset({400, 404, 405, 406, 410, 501})

#: Asked when nothing better is known — the routes services conventionally answer on.
CONVENTIONAL_PATHS: tuple[str, ...] = (
    "/healthz", "/readyz", "/livez", "/health", "/ready", "/status", "/version", "/info",
    "/docs", "/swagger", "/swagger-ui", "/openapi.json", "/api", "/api/health", "/metrics")

#: At most this many routes are asked, and listed.
MAX_PROBES = 30
#: A route answers when it says 2xx or 3xx.
_ANSWERS = range(200, 400)

_SOURCE_EXT = {".go", ".js", ".mjs", ".cjs", ".ts", ".py", ".java", ".kt", ".rb", ".rs", ".cs"}
_CONFIG_EXT = {".yaml", ".yml", ".json", ".toml"}
_SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "target", ".venv", "venv",
              "__pycache__", ".next", "coverage", "testdata", "tests", "test"}
#: A route registered in code: HandleFunc("/x"), router.GET("/x"), app.get('/x'), @app.get("/x"),
#: @GetMapping("/x"), route("/x") … — the call and its first string literal.
_CODE_ROUTE = re.compile(
    r"""(?:HandleFunc|Handle|HandlerFunc|GET|Get|get|Any|route|Route|GetMapping|RequestMapping"""
    r"""|api_route|path|re_path)\s*\(\s*(?:[A-Za-z_.]+\s*,\s*)?(?:value\s*=\s*)?["'`]"""
    r"""(?:GET\s+)?(/[A-Za-z0-9_\-./]*)["'`]""")
#: A route declared in config (a gateway's routes file): `path: /x`, `"prefix": "/x"`, route = "/x".
#: Anchored to the start of a line (after an optional list dash), so `mountPath: /data` or
#: `hostPath:` in a k8s manifest is not a route.
_CONFIG_ROUTE = re.compile(
    r"""^[ \t]*-?[ \t]*["']?(?:path|route|prefix|match|endpoint)["']?[ \t]*[:=][ \t]*["']?"""
    r"""(/[A-Za-z0-9_\-./]*)""",
    re.IGNORECASE | re.MULTILINE)

#: A preview id as BOSS mints it. Anything else is never a preview, never a glob, never a key.
_PREVIEW_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def wants_landing(method: str, subpath: str, status: int, headers: Mapping[str, Any]) -> bool:
    """THE DECISION: a browser loading the preview's root, which the app answered "nothing here".

    Only the root — a 404 on /orders/7 is the app's own answer and must reach the page. Only a
    page load — a script's fetch keeps the app's real status."""
    from .preview_wake import is_page_load

    if str(method or "").upper() != "GET":
        return False
    if str(subpath or "").strip("/") != "":
        return False
    if int(status or 0) not in ROOT_MISS_STATUSES:
        return False
    return is_page_load(headers)


# ── what BOSS knows about the app's routes ──────────────────────────────────────────────────


def _clean(path: Any) -> Optional[str]:
    p = str(path or "").strip()
    if not p.startswith("/") or p.startswith("//") or len(p) > 200:
        return None
    if any(ch in p for ch in "{}:*<>?#$ \t"):    # a template or pattern, not a page to open
        return None
    p = re.sub(r"/{2,}", "/", p)
    if p != "/" and p.endswith("/") and len(p) > 1:
        p = p.rstrip("/") or "/"
    return None if p == "/" else p


def openapi_get_paths(doc: Any) -> list[str]:
    """The GET routes of an OpenAPI/Swagger document that need no parameter to open."""
    paths = (doc or {}).get("paths") if isinstance(doc, dict) else None
    out: list[str] = []
    for raw, ops in (paths or {}).items() if isinstance(paths, dict) else []:
        if isinstance(ops, dict) and "get" in {str(k).lower() for k in ops}:
            base = ""
            for server in (doc.get("servers") or [])[:1]:
                url = str((server or {}).get("url") or "") if isinstance(server, dict) else ""
                if url.startswith("/"):
                    base = url.rstrip("/")
            base = base or str(doc.get("basePath") or "").rstrip("/")
            p = _clean(f"{base}{raw}")
            if p and p not in out:
                out.append(p)
    return out


def _walk(root: Path, exts: set[str], limit: int = 600) -> Iterable[Path]:
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix.lower() in exts:
                seen += 1
                if seen > limit:
                    return
                yield path


def _read(path: Path, cap: int = 256_000) -> str:
    try:
        if path.stat().st_size > cap:
            return ""
        return path.read_text(errors="ignore")
    except OSError:
        return ""


def _routes_from_specs(root: Path) -> list[str]:
    out: list[str] = []
    for path in _walk(root, _CONFIG_EXT, limit=200):
        if not re.match(r"(openapi|swagger)", path.name, re.IGNORECASE):
            continue
        text = _read(path)
        try:
            if path.suffix.lower() == ".json":
                doc = json.loads(text)
            else:
                import yaml

                doc = yaml.safe_load(text)
        except Exception:  # noqa: BLE001 — a broken spec names nothing
            continue
        for p in openapi_get_paths(doc):
            if p not in out:
                out.append(p)
    return out


def openapi_routes(workdir: Optional[Path]) -> list[str]:
    """The GET routes the app's OpenAPI/Swagger documents declare — SAFE to ask."""
    if workdir is None or not Path(workdir).is_dir():
        return []
    return _routes_from_specs(Path(workdir))


def named_routes(workdir: Optional[Path]) -> list[str]:
    """Routes the app's code registers and its config files declare (a gateway's routes table).
    Their METHOD is unknown (HandleFunc("/logout") may mutate), so they are shown, NEVER asked."""
    if workdir is None or not Path(workdir).is_dir():
        return []
    root = Path(workdir)
    out: list[str] = []

    def add(p: Optional[str]) -> None:
        if p and p not in out:
            out.append(p)

    for path in _walk(root, _SOURCE_EXT):
        for m in _CODE_ROUTE.finditer(_read(path)):
            add(_clean(m.group(1)))
    for path in _walk(root, _CONFIG_EXT, limit=200):
        if re.match(r"(openapi|swagger|package|tsconfig|composer)", path.name, re.IGNORECASE):
            continue
        for m in _CONFIG_ROUTE.finditer(_read(path)):
            add(_clean(m.group(1)))
    return out


def target_check_paths(targets: Iterable[Mapping[str, Any]]) -> list[str]:
    """The paths of the health checks the project's deployment targets declare (both shapes,
    through declared_health_probes — the reader the health sweep uses)."""
    from urllib.parse import urlsplit

    from .deployment_connectors import declared_health_probes

    out: list[str] = []
    for t in targets:
        for p in declared_health_probes(dict(t)):
            raw = urlsplit(p["url"]).path if p.get("url") else (p.get("check") or {}).get("path")
            path = _clean(raw)
            if path and path not in out:
                out.append(path)
    return out


def candidate_paths(*, declared: Iterable[str] = (), targets: Iterable[str] = (),
                    openapi: Iterable[str] = ()) -> list[str]:
    """Every route SAFE to ask, most authoritative first: the preview's declared health/ready
    path, the deploy targets' checks, the app's OpenAPI GET routes, then the conventional ones.
    Routes only found in code or config are never here (named_routes: shown, not asked)."""
    out: list[str] = []
    # The spec's routes are capped so the conventional health routes are always asked too.
    for group in (declared, targets, list(openapi)[:MAX_PROBES // 2], CONVENTIONAL_PATHS):
        for p in group:
            c = _clean(p)
            if c and c not in out:
                out.append(c)
    return out[:MAX_PROBES]


#: The whole landing's probing is held to this many seconds; a route still asking is unanswered.
PROBE_BUDGET_SECONDS = 6.0
#: Each route gets this long (passed to the proxy as its timeout).
PROBE_TIMEOUT_SECONDS = 3.0


def answering(paths: list[str], fetch: Callable[[str], int], *, workers: int = 8,
              budget: float = PROBE_BUDGET_SECONDS) -> list[dict[str, Any]]:
    """Ask each path (fetch → HTTP status, 0 when it did not answer) and keep those that answer,
    in the order given — within `budget` seconds overall."""
    def ask(p: str) -> tuple[str, int]:
        try:
            return p, int(fetch(p) or 0)
        except Exception:  # noqa: BLE001 — a route that throws did not answer
            return p, 0

    if not paths:
        return []
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(paths))))
    try:
        futures = [pool.submit(ask, p) for p in paths]
        done, _late = wait(futures, timeout=budget)
        got = dict(f.result() for f in done)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # a slow route never holds the page
    return [{"path": p, "status": got[p]} for p in paths if got.get(p) in _ANSWERS]


# ── the page ─────────────────────────────────────────────────────────────────────────────────


def link_prefix_for(url_path: str) -> str:
    """What a relative link at this URL needs to stay inside the preview: nothing when the URL
    ends in "/", else its last segment ("…/web" → "web/")."""
    path = str(url_path or "/")
    return "" if path.endswith("/") else path.rsplit("/", 1)[-1] + "/"


def landing_html(*, root_status: int, root_body: bytes | str = b"", routes: list[dict[str, Any]],
                 asked: int, link_prefix: str = "", named: Iterable[str] = ()) -> bytes:
    """The page a browser sees at an API preview's root. Links are RELATIVE (`link_prefix` +
    the route without its leading slash), so they open inside the same preview wherever it is
    served — under /v1/previews/<id>/web/ or at the root of the preview origin. `link_prefix`
    is "" when the page's URL ends in "/", else its last segment and a slash (see link_prefix_for)."""
    body = root_body.decode("utf-8", "replace") if isinstance(root_body, bytes) else str(root_body)
    said = " ".join(body.split())[:200]
    if routes:
        items = "".join(
            f"<li><a href='{html.escape(link_prefix + r['path'].lstrip('/'), quote=True)}'>"
            f"<code>{html.escape(r['path'])}</code></a> <span class=s>{int(r['status'])}</span></li>"
            for r in routes)
        listing = (f"<h2>Routes that answer</h2><ul>{items}</ul>"
                   f"<p class=m>BOSS asked {asked} route{'s' if asked != 1 else ''} it knows of "
                   "(declared health checks, the app's own routes, common ones); these answered.</p>")
    else:
        listing = (f"<p>None of the {asked} routes BOSS knows of answered a plain GET. Call the "
                   "API's routes from a client, or declare a health path in "
                   "<code>boss.preview.health</code>.</p>")
    answered = {r["path"] for r in routes}
    others = [p for p in named if p not in answered][:20]
    if others:
        listing += ("<h2>Other routes its code names</h2><p class=m>Not opened by BOSS: their "
                    "method is not known, and some change data.</p><ul>"
                    + "".join(f"<li><code>{html.escape(p)}</code></li>" for p in others)
                    + "</ul>")
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<title>This app is an API</title>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<style>:root{color-scheme:light dark}body{font:15px/1.5 system-ui,sans-serif;"
        "max-width:560px;margin:48px auto;padding:0 16px}h1{font-size:18px}h2{font-size:15px;"
        "margin-top:24px}ul{padding-left:18px}li{margin:4px 0}.s,.m{opacity:.7;font-size:13px}"
        "code{font:13px ui-monospace,monospace}</style></head><body>"
        "<h1>This app is an API — there is no page at /</h1>"
        f"<p>The preview is running. Its root answered <b>HTTP {int(root_status)}</b>"
        + (f" (<code>{html.escape(said)}</code>)" if said else "") +
        ", which is normal for a service that only answers its own routes.</p>"
        + listing + "</body></html>").encode()


# ── one entry point for the proxy ────────────────────────────────────────────────────────────

_CACHE: "OrderedDict[str, tuple[float, list[dict[str, Any]], int, list[str]]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
#: A page reload within this window reuses the last answer rather than asking every route again.
CACHE_SECONDS = 60.0
#: Bounded: one entry per recently opened preview, the oldest dropped first.
CACHE_MAX = 32


def valid_preview_id(preview_id: Any) -> bool:
    return isinstance(preview_id, str) and bool(_PREVIEW_ID.match(preview_id))


def _workdir_of(preview_id: str) -> Optional[Path]:
    """The preview's tree: a directory under PREVIEW_ROOT named exactly `<id>-<suffix>`. Matched
    by prefix on the listed names — the id is never a glob pattern."""
    from .preview_service import PREVIEW_ROOT

    if not valid_preview_id(preview_id):
        return None
    try:
        names = sorted(d for d in PREVIEW_ROOT.iterdir()
                       if d.name.startswith(f"{preview_id}-") and d.is_dir())
    except OSError:
        return None
    return names[0] if names else None


def is_running_app(engine: Any, preview_id: str) -> bool:
    """Did the answer come from an APP? Only for a preview BOSS has a running row for (with an
    address, not a fixture). The gateway's own 404 "preview not found" is not an app's answer."""
    from sqlalchemy import text

    from ..db import begin
    from .preview_service import FIXTURE_MODE

    if not valid_preview_id(preview_id):
        return False
    try:
        with begin(engine) as c:
            row = c.execute(text("SELECT status, url, mode FROM app_previews WHERE id = :id"),
                            {"id": preview_id}).mappings().first()
    except Exception:  # noqa: BLE001
        return False
    return bool(row and row["status"] == "running" and row["url"]
                and str(row["mode"] or "") != FIXTURE_MODE)


def _known(engine: Any, preview_id: str) -> tuple[list[str], list[str]]:
    """(routes safe to ask, routes only named by the code/config). Best-effort; never raises."""
    from sqlalchemy import text

    from ..db import begin
    from .preview_service import preview_health_path

    workdir = _workdir_of(preview_id)
    declared = [preview_health_path(workdir)] if workdir else []
    targets: list[str] = []
    try:
        with begin(engine) as c:
            row = c.execute(text("SELECT project_id, repo FROM app_previews WHERE id = :id"),
                            {"id": preview_id}).mappings().first()
            if row:
                rows = c.execute(text(
                    "SELECT domains, health_checks, public_url, target_ref FROM deployment_targets "
                    "WHERE project_id = :p AND status = 'active' "
                    "AND (CAST(:r AS TEXT) IS NULL OR repo = :r OR repo IS NULL)"),
                    {"p": row["project_id"], "r": row["repo"]}).mappings().all()
                targets = target_check_paths(dict(r) for r in rows)
    except Exception:  # noqa: BLE001
        targets = []
    safe = candidate_paths(declared=declared, targets=targets, openapi=openapi_routes(workdir))
    return safe, named_routes(workdir)


def landing_for(engine: Any, preview_id: str, *, root_status: int, root_body: bytes,
                fetch: Callable[[str], int], link_prefix: str = "") -> Optional[bytes]:
    """The landing page for one RUNNING preview, or None (the app's own answer stands). Known
    safe routes are asked through `fetch` within the probe budget; the answer is cached briefly
    in a small LRU. Blocking: the gateway runs it off the event loop."""
    if not is_running_app(engine, preview_id):
        return None
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(preview_id)
        if hit:
            _CACHE.move_to_end(preview_id)
    if hit and now - hit[0] < CACHE_SECONDS:
        routes, asked, named = hit[1], hit[2], hit[3]
    else:
        paths, named = _known(engine, preview_id)
        routes, asked = answering(paths, fetch), len(paths)
        with _CACHE_LOCK:
            _CACHE[preview_id] = (now, routes, asked, named)
            _CACHE.move_to_end(preview_id)
            while len(_CACHE) > CACHE_MAX:
                _CACHE.popitem(last=False)
    return landing_html(root_status=root_status, root_body=root_body, routes=routes,
                        asked=asked, link_prefix=link_prefix, named=named)


__all__ = ["CONVENTIONAL_PATHS", "ROOT_MISS_STATUSES", "answering", "candidate_paths", "link_prefix_for",
           "is_running_app", "landing_for", "landing_html", "named_routes", "openapi_get_paths",
           "openapi_routes", "target_check_paths", "valid_preview_id", "wants_landing"]
