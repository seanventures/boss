"""Tier-1 "1-box" app previews.

After a code task produces a green PR, an operator wants to *see the working website*
before merging. This runs the built app from its branch as a managed, auto-expiring local
process on the dev box and hands back a live preview URL — zero extra compute.

Process-based (not containerized) for v1: simplest, works with loopback-binding apps on
macOS where container host-networking is awkward. Containerized isolation is the v2 hardening.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import shlex
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from ..config import Settings
from . import owned_process
from ..db import begin
from .code_task_service import load_code_task_run
from .core import emit_event, make_id
from .verification_target import NPM_INSTALL_CMD
from .github_connections import github_token_for
from .execution_substrate import ExecutionSubstrate, normalize_build_substrate
from .sandbox_provider import MicroVMProvider

from .scheduling import low_priority

logger = logging.getLogger(__name__)

PREVIEW_ROOT = Path(os.environ.get("BOSS_PREVIEW_ROOT", "/tmp/boss-previews"))


# ------------------------------------------------------------------------------------------ #
# THE PREVIEW SANDBOX. A preview runs an app an agent (or a template, or a deliberately insecure
# demo) wrote. It ran as the control plane's own uid, so it could read whatever the API can: the
# identity master key, /proc/<api>/environ (DATABASE_URL, provider keys), every other run's
# directory, the database over the network. Now every process preview runs the way a build does
# (sandbox_identity, IDENTITY.md §7.6): its own pool uid, leased for the preview's life
# (`acquire`, released by `_teardown`), its tree inside that uid's 2770 run directory, the
# sandbox environment (no control-plane variable), and the entrypoint's network fence: loopback
# and its OWN preview port, the API and egress (allowlisted outbound) and the link gateway; never
# another preview's port, the database or anything else. Its log lives OUTSIDE its tree (the app
# could otherwise swap the log for a link to a BOSS file and have BOSS read it back).
# ------------------------------------------------------------------------------------------ #

def _preview_root() -> Path:
    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(PREVIEW_ROOT, 0o700)
    return PREVIEW_ROOT


def preview_log_path(preview_id: str) -> str:
    """Where a preview's output goes: BOSS's directory, never the app's tree."""
    return str(_preview_root() / f"{preview_id}.log")


def _preview_sandbox(preview_id: str):
    """The preview's own sandbox uid, held until `_teardown` (None when isolation is off, or
    `auto` without the helper). Raises SandboxUnavailable when isolation is required and
    impossible: the preview then fails, it never runs as the control plane."""
    from . import sandbox_identity

    return sandbox_identity.acquire(preview_id, what="preview")


def _release_preview_sandbox(preview_id: Optional[str]) -> None:
    if not preview_id:
        return
    with contextlib.suppress(Exception):
        from . import sandbox_identity

        sandbox_identity.release(str(preview_id))
    # Its throwaway database ends with it (after its processes: nothing still writes). The
    # reaper drops one this process no longer holds (preview_database_ids keeps live ones).
    with contextlib.suppress(Exception):
        from . import build_services

        build_services.teardown(str(preview_id), reason="preview_ended")


def preview_needs_database(run: Optional[dict], workdir: Optional[Path]) -> list[str]:
    """Why this preview needs its own Postgres, or [] when it does not.

    A PREVIEW OF A BUILD THAT HAD A DATABASE GETS ONE TOO. The build ran its tests against its
    throwaway Postgres (build_services) and passed, then the preview started the same app with
    no DATABASE_URL: it exited on ECONNREFUSED 127.0.0.1:5432, the preview step failed, and the
    deploy step — which requires a reviewed preview — could never run. Same rule as the build:
    the build's recorded decision (state.build_database), else the app's own code says Postgres;
    the project's Never wins."""
    state = (run or {}).get("state") or {}
    if isinstance(state, str):
        try:
            state = json.loads(state)
        except ValueError:
            state = {}
    decided = state.get("build_database") if isinstance(state, dict) else None
    decided = decided if isinstance(decided, dict) else {}
    from . import build_services

    if decided.get("mode") == build_services.NEVER:
        return []
    if decided.get("postgres"):
        return ["the build ran with a Postgres database"] + [str(w) for w in (decided.get("why") or [])][:3]
    if workdir is not None:
        try:
            return build_services.detect_in_code(workdir)[:3]
        except Exception:  # noqa: BLE001 — detection is a hint; the build's record decides
            logger.debug("preview database detection failed", exc_info=True)
    return []


def provision_preview_database(engine, run: Optional[dict], preview_id: str,
                               workdir: Optional[Path]) -> Optional[str]:
    """Give preview `preview_id` its own throwaway database when it needs one. Returns None when
    it is ready or not needed, else why it could not be made (the preview fails with that reason
    instead of starting an app that cannot reach its database).

    The lease is keyed by the PREVIEW id, so owned_process injects DATABASE_URL / PG* into the
    preview's own processes only (Owner("preview", preview_id) — `preview` is a workload kind),
    after the app's environment filter; the password is scrubbed like a build's."""
    why = preview_needs_database(run, workdir)
    if not why:
        return None
    from . import build_services

    install = ""
    with contextlib.suppress(Exception):
        from .resource_ledger import install_id

        with begin(engine) as c:
            install = install_id(c)
    try:
        lease = build_services.provision(preview_id, install=install)
    except build_services.BuildServicesUnavailable as exc:
        return f"this app needs PostgreSQL ({why[0]}), but its preview database could not be made: {exc}"
    with contextlib.suppress(Exception), begin(engine) as c:
        project_id = (run or {}).get("project_id") or ""
        emit_event(c, project_id, "app_preview.database_ready", "boss", "app_preview", preview_id,
                   {"run_id": (run or {}).get("id"), "database": lease.name, "host": lease.host,
                    "port": lease.port, "env": list(build_services.ENV_NAMES), "why": why})
    return None


def _preview_workdir(preview_id: str, ident) -> Path:
    if ident is not None:
        return ident.workspace(f"{preview_id}-")
    return Path(tempfile.mkdtemp(prefix=f"{preview_id}-", dir=str(_preview_root())))


def _open_to_sandbox(root: Path) -> None:
    """What BOSS copied into a sandboxed preview's tree becomes editable by its uid (npm install
    writes beside the sources). Only that uid and BOSS can enter the 2770 run directory, so this
    opens nothing to anyone else. Never follows a symlink."""
    from . import workspace_perms

    workspace_perms.open_tree(root)  # executables stay executable (workspace_perms.usable_mode)


def _sandboxed_preview_port(ident) -> int:
    """The port the fence opens to this preview's own uid (BOSS_PREVIEW_SELF), when free; any
    free preview port otherwise (the app then cannot call itself on its port)."""
    if ident is not None:
        from .sandbox_identity import preview_port_for_uid

        own = preview_port_for_uid(ident.uid)
        if own is not None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                # As servers bind (SO_REUSEADDR): the last preview's closed connections in
                # TIME_WAIT do not make the port busy; a live listener still does.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("0.0.0.0", own))
                    return own
                except OSError:
                    pass
    return app_preview_port()


def ports_listened_by(uid: int, tables: tuple[str, ...] = ("/proc/net/tcp", "/proc/net/tcp6")
                      ) -> Optional[set[int]]:
    """TCP ports with a LISTEN socket owned by `uid` (/proc/net/tcp, tcp6), or None where /proc
    cannot say (a developer's Mac: no sandbox there either)."""
    found: set[int] = set()
    seen_any = False
    for table in tables:
        try:
            with open(table, encoding="ascii", errors="ignore") as fh:
                next(fh, None)
                seen_any = True
                for line in fh:
                    f = line.split()
                    if len(f) > 7 and f[3] == "0A" and f[7] == str(int(uid)):
                        found.add(int(f[1].rsplit(":", 1)[1], 16))
        except OSError:
            continue
    return found if seen_any else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_loopback_url(url: Optional[str]) -> bool:
    if not url:
        return False
    lowered = url.strip().lower()
    return (
        "://127." in lowered
        or "://localhost" in lowered
        or "://[::1]" in lowered
        or "://0.0.0.0" in lowered
        or lowered.startswith("127.")
        or lowered.startswith("localhost")
        or lowered.startswith("[::1]")
        or lowered.startswith("0.0.0.0")
    )


def _preview_public_base(
    explicit: Optional[str] = None, *, settings: Optional[Settings] = None
) -> str:
    """Operator-facing origin for View URLs. Loopback bases are rejected (fail closed
    to relative gateway paths) so a mis-set BOSS_PREVIEW_PUBLIC_BASE cannot reintroduce
    127.0.0.1 into operator-facing URLs.

    An explicit ``public_base`` wins (and a loopback explicit value still fail-closes).
    Otherwise prefer ``settings.preview_public_base``, then ``BOSS_PREVIEW_PUBLIC_BASE``
    in the process environment so a Settings object constructed without the field
    (or with an empty default) cannot hide a live-set public origin.
    """
    if explicit is not None:
        raw = explicit
    else:
        raw = ""
        if settings is not None:
            raw = getattr(settings, "preview_public_base", None) or ""
        if not str(raw or "").strip():
            raw = os.environ.get("BOSS_PREVIEW_PUBLIC_BASE", "") or ""
    base = (raw or "").strip().rstrip("/")
    if not base:
        return ""
    if _is_loopback_url(base):
        return ""
    return base


# THE PREVIEW ORIGIN (security finding #4). A previewed app is someone else's code; served on
# the console's origin, one path spelling the console's matcher missed (`/v1/previews/x/%77eb/`)
# dropped the sandbox and the app read the CSRF token from /v1/identity/me. So previews no longer
# share an origin with the console at all: apps/api/app/preview_origin.py serves ONLY the preview
# capability paths on their own port, stripped of every cookie and credential header, and the
# console redirects any preview path it receives to that origin (app/v1/[...path]/route.ts).
#
#   BOSS_PREVIEW_ORIGIN  the absolute origin a browser opens previews at, e.g.
#                        https://previews.example.com. Loopback is fine here: it is a different
#                        origin from a loopback console by its port. Set it behind TLS.
#   BOSS_PREVIEW_PORT    the port the preview-only listener binds (default 3001; 0 or off = none).
#                        With BOSS_PREVIEW_ORIGIN unset, the origin is the console's own host on
#                        this port (console http://localhost:3000 -> previews http://localhost:3001).
PREVIEW_ORIGIN_ENV = "BOSS_PREVIEW_ORIGIN"
PREVIEW_PORT_ENV = "BOSS_PREVIEW_PORT"
DEFAULT_PREVIEW_PORT = 3001


def preview_port() -> Optional[int]:
    """The preview-only listener's port, or None when it is switched off."""
    raw = (os.environ.get(PREVIEW_PORT_ENV) or "").strip().lower()
    if raw in ("0", "off", "false", "no", "none"):
        return None
    if not raw:
        return DEFAULT_PREVIEW_PORT
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_PREVIEW_PORT
    return port if 0 < port < 65536 else DEFAULT_PREVIEW_PORT


def _origin_key(origin: str) -> Optional[tuple[str, str, int]]:
    """(scheme, host, port) of an origin, the default port filled in; None when it is not one."""
    parts = urllib.parse.urlsplit(str(origin or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None
    return parts.scheme, parts.hostname.lower(), int(port)


def boss_origins(host_header: Optional[str] = None,
                 scheme: str = "http") -> set[tuple[str, str, int]]:
    """BOSS'S OWN ORIGINS: the console/API a browser reached (`host_header`), and BOSS_PUBLIC_URL.
    A preview origin must never be one of them (see PREVIEW_ORIGIN_SANDBOX_CSP)."""
    out: set[tuple[str, str, int]] = set()
    if host_header:
        key = _origin_key(f"{'https' if scheme == 'https' else 'http'}://{host_header.strip()}")
        if key:
            out.add(key)
    try:
        from ..config import get_settings

        key = _origin_key(str(get_settings().public_url or ""))
        if key:
            out.add(key)
    except Exception:  # noqa: BLE001 — no settings: the request's own origin still counts
        pass
    return out


def is_boss_origin(origin: str, host_header: Optional[str] = None, scheme: str = "http") -> bool:
    key = _origin_key(origin)
    return key is not None and key in boss_origins(host_header, scheme)


def configured_preview_origin() -> str:
    """BOSS_PREVIEW_ORIGIN as a bare origin (scheme://host[:port]), or "" when unset/invalid —
    or when it names BOSS's own public origin: a preview is never served where BOSS is."""
    raw = (os.environ.get(PREVIEW_ORIGIN_ENV) or "").strip()
    if not raw:
        return ""
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return ""
    origin = f"{parts.scheme}://{parts.netloc}"
    if is_boss_origin(origin):
        logger.warning("BOSS_PREVIEW_ORIGIN %s is BOSS's own origin; previews are not served "
                       "there", origin)
        return ""
    return origin


_PREVIEW_LABEL_ID = re.compile(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*")


def preview_host_label(preview_id: str) -> str:
    """The DNS label for one preview. An underscore is not legal in a host, so it is written as a
    hyphen (`prev_aaa` → `prev-aaa`). An id that already contains a hyphen has no label: it must
    not collapse onto the underscored id."""
    if not _PREVIEW_LABEL_ID.fullmatch(str(preview_id or "")):
        return ""
    label = str(preview_id).lower().replace("_", "-")
    return label if 1 <= len(label) <= 63 else ""


def _ipv4(host: str) -> bool:
    parts = host.split(".")
    return len(parts) == 4 and all(part.isdigit() and int(part) <= 255 for part in parts)


def per_preview_origin(base: str, preview_id: str) -> str:
    """`base` with this preview's label in front of the host, so two previews are two origins.
    An IP address cannot take a label; the parent becomes localhost, which browsers resolve."""
    label = preview_host_label(preview_id)
    parts = urllib.parse.urlsplit(str(base or "").strip())
    if not label or parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname.lower()
    try:
        port = parts.port
    except ValueError:
        return ""
    parent = "localhost" if ":" in host or _ipv4(host) else host
    netloc = f"{label}.{parent}" + (f":{port}" if port else "")
    origin = f"{parts.scheme}://{netloc}"
    return "" if is_boss_origin(origin) else origin


def preview_origin_for(host_header: Optional[str], scheme: str = "http",
                       preview_id: Optional[str] = None) -> str:
    """Where a browser that reached BOSS at `host_header` opens previews: BOSS_PREVIEW_ORIGIN, or
    the same host on BOSS_PREVIEW_PORT. "" when neither can be said (no port, no usable host) —
    and "" when the answer would be BOSS's own origin (BOSS_PREVIEW_PORT set to the console's
    port, BOSS_PREVIEW_ORIGIN naming the console): a preview page may keep its own cookies
    (allow-same-origin) only because it never shares an origin with BOSS.

    With a `preview_id`, the origin is that preview's own host (`<label>.localhost:3001`), never
    the one host every preview used to share (BOSSAPP-947)."""
    configured = configured_preview_origin()
    if configured:
        origin = "" if is_boss_origin(configured, host_header, scheme) else configured
    else:
        port = preview_port()
        if port is None:
            return ""
        # A Host header is `name[:port]` or `[v6][:port]`, nothing else: anything more is refused.
        found = re.fullmatch(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.\-]+)(?::\d{1,5})?", (host_header or "").strip())
        if not found:
            return ""
        hostname = found.group(1)
        origin = f"{'https' if scheme == 'https' else 'http'}://{hostname}:{port}"
        if is_boss_origin(origin, host_header, scheme):
            return ""
    if not origin:
        return ""
    return per_preview_origin(origin, preview_id) if preview_id else origin


def public_preview_url(preview_id: str, host_header: Optional[str] = None, scheme: str = "http",
                       *, settings: Optional[Settings] = None) -> str:
    """The ABSOLUTE address a person's browser (or a provider calling back) reaches this
    preview at — the preview origin, else BOSS_PREVIEW_PUBLIC_BASE — or "" when BOSS cannot
    say one. Never `app_previews.url`: that is the app's port inside the API container
    (http://127.0.0.1:19769), reachable from nowhere a browser or a webhook runs."""
    from urllib.parse import quote

    path = f"/v1/previews/{quote(str(preview_id), safe='')}/web"
    origin = preview_origin_for(host_header, scheme, preview_id)
    if origin:
        return origin + path
    base = _preview_public_base(None, settings=settings)
    return base + path if base else ""


def operator_preview_open_url(
    preview_id: str,
    *,
    public_base: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> str:
    """URL a browser can open for the preview (reverse-proxied by the BOSS API).

    The preview process binds 127.0.0.1 inside the API host; only this gateway path is
    reachable from an operator browser or agent on the tailnet. When
    ``BOSS_PREVIEW_PUBLIC_BASE`` (or ``public_base``) is set to a non-loopback origin,
    returns an absolute URL; otherwise a relative gateway path.
    """
    path = f"/v1/previews/{preview_id}/web/"
    # The preview origin first: it is a separate origin even on loopback (another port).
    preview_origin = configured_preview_origin() if public_base is None else ""
    if preview_origin:
        # The configured host is the listener. The page itself is this preview's host on it.
        labeled = per_preview_origin(preview_origin, preview_id)
        return f"{labeled}{path}" if labeled else path
    base = _preview_public_base(public_base, settings=settings)
    if base:
        return f"{base}{path}"
    return path


def publicize_preview(
    record: dict,
    *,
    public_base: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> dict:
    """Never hand out the preview's internal address as the primary ``url``.

    ``app_previews.url`` holds the sandbox-internal address
    (``http://127.0.0.1:43053``), reachable from the API process and from nowhere a
    browser runs. Listing that column as ``url`` is how operators and agents were
    told to open an unreachable address.

    The internal address is preserved under ``internal_url`` for diagnostics and
    for the in-process reverse proxy, which still probes the loopback port.

    When ``BOSS_PREVIEW_PUBLIC_BASE`` is set, operator-facing ``url`` / ``open_url``
    become absolute under that origin. A loopback public base is rejected.
    """
    out = dict(record)
    preview_id = out.get("id")
    internal = out.get("url")
    if internal is not None:
        out["internal_url"] = internal
    if not preview_id:
        # Still strip loopback from primary url if someone handed us a bare record.
        if _is_loopback_url(out.get("url")):
            out["url"] = None
        return out
    # Any record that ever had a live bind (or is still marked running) gets a
    # gateway open URL as the primary `url`. Failed/never-started rows with no
    # bind stay url=null so the console does not offer a dead "Open" link.
    if internal is not None or str(out.get("status") or "").lower() == "running":
        open_url = operator_preview_open_url(
            str(preview_id), public_base=public_base, settings=settings
        )
        out["open_url"] = open_url
        out["url"] = open_url
    # Hard invariant: primary operator URL must never be loopback, even if a
    # non-gateway absolute slipped into the DB column without publicize rewrite.
    if _is_loopback_url(out.get("url")):
        open_url = operator_preview_open_url(
            str(preview_id), public_base=public_base, settings=settings
        )
        out["open_url"] = open_url
        out["url"] = open_url
    return out


FIXTURE_MODE = "fixture"

FIXTURE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>BOSS preview fixture</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 36rem; margin: 3rem auto; padding: 0 1rem; }
    .badge { display: inline-block; border: 1px solid #888; border-radius: 999px; padding: 2px 10px; font-size: 12px; }
  </style>
</head>
<body>
  <p class="badge">fixture — not a sandboxed app</p>
  <h1>Preview View path is live</h1>
  <p>This page is served by the BOSS API gateway at <code>/v1/previews/{preview_id}/web/</code>.
     It is a no-spend fixture so operators can prove View HTML without a microVM rootfs.</p>
  <p data-testid="preview-fixture">ok</p>
</body>
</html>
"""


def create_fixture_preview(
    engine,
    settings: Settings,
    *,
    project_id: str,
    actor: str,
    ttl_minutes: int = 60,
) -> dict:
    """No-spend running preview that serves fixture HTML through the gateway.

    Does not clone a repo, start a process, or claim a microVM. Status is
    ``running`` with ``mode=fixture`` so View HTML can be proven while sandbox
    claims stay frozen. Never advertised as a real app preview.
    """
    preview_id = make_id("prev")
    ttl = max(5, min(int(ttl_minutes), 180))
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl)
    with begin(engine) as connection:
        connection.execute(
            text(
                """
                INSERT INTO app_previews (
                    id, project_id, run_id, repo, branch, command, port, url,
                    mode, substrate, status, error, expires_at
                )
                VALUES (
                    :id, :project_id, NULL, NULL, NULL, 'fixture', NULL, NULL,
                    :mode, 'preview_host', 'running',
                    'fixture — not a sandboxed app', :expires
                )
                """
            ),
            {
                "id": preview_id,
                "project_id": project_id,
                "mode": FIXTURE_MODE,
                "expires": expires,
            },
        )
        emit_event(
            connection,
            project_id,
            "preview.fixture_started",
            actor,
            "app_preview",
            preview_id,
            {"mode": FIXTURE_MODE, "ttl_minutes": ttl, "simulated": True},
        )
        row = connection.execute(
            text("SELECT * FROM app_previews WHERE id = :id"),
            {"id": preview_id},
        ).mappings().first()
    record = publicize_preview(dict(row), settings=settings)
    record["fixture"] = True
    record["simulated"] = True
    return record


def fixture_html(preview_id: str) -> bytes:
    return FIXTURE_HTML.replace("{preview_id}", preview_id).encode("utf-8")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def app_preview_port() -> int:
    """A free port for a PREVIEW's app, from BOSS_PREVIEW_PORTS (default 19000-19999). Previews live in
    their own range so the sandbox fence can refuse it to every build (a build reaches another
    app's preview only through the link gateway, F5 §5.4) and the link gateway can refuse
    anything outside it as an upstream. Falls back to any free port only if the range is full."""
    import random

    from ..app_links.service import preview_ports

    pool = preview_ports()
    span = pool.high - pool.low + 1
    start = random.randrange(span)
    for i in range(span):
        port = pool.low + (start + i) % span
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                continue
        return port
    logger.warning("BOSS_PREVIEW_PORTS is full; this preview takes an ephemeral port")
    return _free_port()


def preview_link_environment(engine, *, project_id: str, run_id: Optional[str], preview_id: str,
                             expires_at, rotate: bool, app_env=None) -> dict[str, str]:
    """What cross-app links add to a preview (F5 §5.3): as a consumer, BOSS_LINK_<ALIAS>_* and
    this preview's link pass; as a provider, BOSS_TEST_USERS (rotated on a NEW preview). Never
    fails the preview: a link that cannot be resolved is logged by name and left out. Also the
    app's own environment (`app_env`, a preview_app_environment; links win on a name clash)."""
    if app_env is None:
        app_env = preview_app_environment(engine, project_id=project_id, run_id=run_id,
                                          preview_id=preview_id)
    try:
        from ..app_links import service as _links

        with begin(engine) as c:
            env = _links.workload_environment(c, project_id=project_id, run_id=run_id, env="preview",
                                              consumer_kind="preview", consumer_id=preview_id,
                                              expires_at=expires_at, rotate=rotate)
    except Exception as exc:  # noqa: BLE001
        logger.warning("preview %s starts without its link environment (%s)", preview_id, type(exc).__name__)
        return dict(app_env.values)
    if env:
        logger.info("preview %s gets %s", preview_id, ", ".join(sorted(env)))
    return {**app_env.values, **env}


def _read_log(log_path: str) -> str:
    try:
        return Path(log_path).read_text(errors="ignore")
    except OSError:
        return ""


def _app_dir_of_plan(workdir: Path, plan: Optional["AppRun"]) -> Path:
    rel = "."
    if plan is not None:
        if plan.declared_in:
            rel = plan.declared_in
        elif plan.entry and plan.entry[0] in ("node", "static", "java") and plan.entry[1]:
            rel = plan.entry[1]
    return workdir if rel in ("", ".") else workdir / rel


def app_dir_of(workdir: Path, changed: Optional[list[str]] = None) -> Path:
    """The directory of the app a runner starts (where its .env.example and boss.deploy.yaml
    live): the declared command's directory, a resolved Node/static app's, else the root."""
    try:
        plan = how_app_runs(workdir, changed)
    except Exception:  # noqa: BLE001 — an ambiguous repo is the launcher's to report
        plan = None
    return _app_dir_of_plan(workdir, plan)


def record_demo_logins(engine, preview_id: str, app_env) -> list[str]:
    """Name, on the preview's row, the demo logins it signs in with (run_env.RunEnvironment.shown:
    a demo/seed password BOSS generated), so its card can say where the reviewer reads the
    password (the app's Setup › Demo logins). Names only. Never fails the preview."""
    names = list(getattr(app_env, "shown", None) or [])
    if not names:
        return []
    try:
        with begin(engine) as c:
            c.execute(text("UPDATE app_previews SET demo_logins = CAST(:d AS JSONB) WHERE id = :id"),
                      {"id": preview_id, "d": json.dumps(names)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("preview %s: demo logins not recorded (%s)", preview_id, type(exc).__name__)
    return names


def preview_app_environment(engine, *, project_id: str, run_id: Optional[str], preview_id: str,
                            app_dir: Optional[Path] = None):
    """What the app declares, resolved for a preview (oidc_provider.app_sign_in.
    preview_run_environment, run_env's rule): its `preview` secrets (never live), a FRESH value
    for each secret BOSS generates, APP_BASE_URL, and its Sign in with BOSS redirect URI. Never
    fails the preview: an error is logged by kind and the app's own files still count."""
    from ..templates.run_env import needs_from_app_dir, resolve

    try:
        from ..oidc_provider.app_sign_in import preview_run_environment

        with begin(engine) as c:
            env = preview_run_environment(c, project_id=project_id, run_id=run_id,
                                          preview_id=preview_id, app_dir=app_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("preview %s starts without its app secrets (%s)", preview_id, type(exc).__name__)
        env = resolve(needs_from_app_dir(app_dir), purpose="preview")
    # The preview's own database (provision_preview_database, keyed by the preview id) wins over
    # "listed/unset", as the build's does in its proof.
    env.supply(_build_services_env(owned_process.Owner("preview", preview_id)))
    if env.values:
        logger.info("preview %s gets %s", preview_id, ", ".join(sorted(env.values)))
    return env


def _low_priority(command: list[str]) -> list[str]:
    """Run a preview build at LOW CPU/IO priority.

    The diagnosis this docstring used to carry — `npm install` + `vite build` run inside the api
    container and at normal priority starve it into returning 404s ("Reconnecting to the control
    plane") — was RIGHT, and was applied to preview builds only. The code agent and the sandbox
    do the same work for far longer and were never given the same treatment.

    So the rule moved to services/scheduling.py and all three call it. This wrapper stays because
    it is the name the preview paths already use.
    """
    return low_priority(command)


# Single source of truth for "which python file starts this app". Kept as one constant because
# the host path (_detect_command) and the container path (_generate_dockerfile) MUST agree --
# they previously carried separate copies of this tuple, and an app whose entrypoint was named
# serve.py failed to launch in both while the test fallback reported a misleading 'verified'.
_PY_ENTRYPOINTS = ("server.py", "serve.py", "app.py", "wsgi.py", "main.py")
# Directories a static build drops its index.html into, in priority order ("." = repo root).
_STATIC_DIRS = (".", "web", "public", "dist", "site", "static")
# Basename prefixes that SUGGEST a Python entrypoint. A suggestion only: see
# python_source_is_program for why a name is never enough on its own.
_PY_ENTRY_PREFIXES = ("serve", "server", "app", "main", "wsgi")

# Calls that construct or start an HTTP server/app object. Matched on the called name's last
# component, so `http.server.ThreadingHTTPServer(...)` and `ThreadingHTTPServer(...)` both count.
_PY_SERVER_CALLS = frozenset({
    "Flask", "FastAPI", "Starlette", "Quart", "Sanic", "Bottle", "Application",
    "HTTPServer", "ThreadingHTTPServer", "TCPServer", "ThreadingTCPServer",
    "make_server", "serve_forever", "run_app", "run_simple",
})


def _called_name(node) -> str:
    import ast
    func = node.func if isinstance(node, ast.Call) else None
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def python_source_is_program(source: str) -> bool:
    """True when running this file as a script would DO something, not merely define things.

    WHY. lead_triage_core/approvals.py was previewed as the Lead Triage Inbox app (boss-1,
    crun_3e370c114b95425d8a7c, PR paramg/boss-sandbox#218) because its name starts with "app".
    It is a library module: imports, dataclasses, functions — and relative imports, so running
    it as `python3 lead_triage_core/approvals.py` died at once with "ImportError: attempted
    relative import with no known parent package". The preview waited for a port that nothing
    would ever open and reported a timeout, twice.

    A name says what a file is CALLED. Whether it is a program is in its body: an
    `if __name__ == "__main__":` block, a top-level call that runs something (`app.run()`,
    `serve()`, `print(...)`), or construction of a known server/app object (`Flask(__name__)`,
    `ThreadingHTTPServer(...)`). A module of definitions is none of those, whatever its name.

    A file that does not parse is judged by the `__main__` guard alone, textually — a syntax
    error is the build's problem to report, not a reason to stop recognising its entrypoint.
    """
    import ast
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError):
        return bool(re.search(r"""__name__\s*==\s*['"]__main__['"]""", source or ""))
    for node in tree.body:
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"):
                return True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            return True
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
                getattr(node, "value", None), ast.Call):
            if _called_name(node.value) in _PY_SERVER_CALLS:
                return True
    return False


def python_source_has_main_guard(source: str) -> bool:
    """True when the file has a top-level `if __name__ == "__main__":` block."""
    import ast
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError):
        return bool(re.search(r"""__name__\s*==\s*['"]__main__['"]""", source or ""))
    return any(isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
               and isinstance(n.test.left, ast.Name) and n.test.left.id == "__name__"
               for n in tree.body)


def python_entry_strength(source: str) -> int:
    """How surely a program is an app: 0 serves HTTP, 1 has a __main__ block, 2 merely calls.

    python_source_is_program counts ANY top-level call, so a config module app_settings.py that
    runs load_dotenv() is "a program" — and in changed-list order it beat the server.py the same
    build wrote. Being a program qualifies a candidate; this ranks the qualified ones.
    """
    if python_source_serves(source):
        return 0
    return 1 if python_source_has_main_guard(source) else 2


def python_source_serves(source: str) -> bool:
    """True when RUNNING this file would construct or start an HTTP server/app object.

    Reachable from what runs, not merely present. This walked the whole file, so a CLI whose
    `serve` subcommand handler built a ThreadingHTTPServer counted as a server; the serve proof
    then ran it bare, argparse exited 2 with its usage line, and a CLI build was blocked by a
    gate nothing may ship past (second review of f16b5d4). Code that runs is the module's
    top-level statements (its `__main__` block included) plus every top-level function or class
    they call BY NAME, followed transitively. A handler reached only through `args.func(args)`
    or a subcommand table is not reached, and that is the point: a bare start never runs it.
    """
    import ast
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError):
        return False
    _defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    defined = {n.name: n for n in tree.body if isinstance(n, _defs)}
    pending = [n for n in tree.body if not isinstance(n, _defs)]
    entered: set[str] = set()
    while pending:
        for node in ast.walk(pending.pop()):
            if not isinstance(node, ast.Call):
                continue
            if _called_name(node) in _PY_SERVER_CALLS:
                return True
            func = node.func
            if isinstance(func, ast.Name) and func.id in defined and func.id not in entered:
                entered.add(func.id)
                pending.append(defined[func.id])
    return False


def _read_py(workdir: Path, rel: str) -> Optional[str]:
    try:
        return (workdir / rel).read_text(errors="ignore")
    except OSError:
        return None


def _is_py_test_path(rel: str) -> bool:
    p = "/" + rel.replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    return (any(seg in p for seg in ("/tests/", "/test/", "/e2e/"))
            or base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py")


def python_entry_invocation(workdir: Path, rel: str) -> tuple[str, list[str]]:
    """(directory to run from, argv) for ONE Python entry file. The only place that decides it.

    A module inside a package (its directory holds __init__.py) is run as
    `python3 -m pkg.module` from the directory ABOVE the outermost package, which is what makes
    its relative imports resolve. `python3 pkg/module.py` puts pkg/ itself on sys.path with no
    parent package, and any `from .x import y` in it dies with "attempted relative import with
    no known parent package" — the error the Lead Triage Inbox preview hit (crun_3e370c11).
    A file that is not in a package runs as a plain script, exactly as before.
    """
    rel = rel.strip()
    if rel.startswith("./"):
        rel = rel[2:]
    path = Path(rel)
    parts = list(path.parent.parts)
    pkg_start = len(parts)
    while pkg_start > 0 and (workdir.joinpath(*parts[:pkg_start]) / "__init__.py").is_file():
        pkg_start -= 1
    if pkg_start == len(parts):
        return ".", ["python3", rel]
    module = ".".join(parts[pkg_start:] + [path.stem])
    run_dir = str(Path(*parts[:pkg_start])) if pkg_start else "."
    return run_dir, ["python3", "-m", module]



# The file a build writes to say WHICH app it produced, as "<kind>:<value>" (e.g.
# "py:serve_scorecard.py", "node:bugbox", "static:web-nqueens"). See declare_app_entry.
DECLARED_APP_FILE = "boss.preview.app"


def declare_app_entry(paths, read=None) -> Optional[str]:
    """The app entry a build produced, derived ONLY from the files that build wrote.

    WHY THIS EXISTS — the fix for a bug fixed five times.

    Every previous fix taught resolve_app_entry one more repo shape, and the next shape broke
    it again: a task that added serve_nqueens.py previewed SUDOKU (PR #62); a task that wrote
    bugbox/ previewed the repo root (PR #161); a Python task previewed a vite root that had no
    entry module. The constant is not the shapes, it is that the preview RE-DERIVES a fact the
    build already knew, from a repository that has accumulated one more app on every run — nine
    runnable apps by the time the interview-scorecard build ran, ten by the next.

    The signal is strongest at build time and decays afterwards. So the decision is made once,
    here, against this run's OWN files, and committed to the branch. Later previews read it
    instead of guessing, and no amount of accumulation in the repository can mislead them.

    Returns None when this run's files do not clearly identify one app — a build that touched
    only tests or docs, or one that somehow produced two servers. None means "say nothing and
    let the existing resolver try", never "guess".
    """
    # Contents known means programs can be told from modules (python_source_is_program); a
    # caller with only paths gets the older name-only rule rather than nothing.
    contents_known = read is not None
    read = read or (lambda _p: "")
    owned = [str(p).strip().lstrip("./") for p in (paths or []) if str(p).strip()]

    def _dir_of(path: str) -> str:
        head, _, _tail = path.rpartition("/")
        return head or "."

    # A server this run wrote outranks static assets the same run wrote: serving an app's
    # index.html without its API is a page that loads and does nothing.
    node_dirs, py_entries, static_dirs = [], [], []
    for path in owned:
        name = path.rpartition("/")[2]
        if name == "package.json":
            try:
                scripts = (json.loads(read(path) or "{}") or {}).get("scripts") or {}
            except (ValueError, TypeError):
                scripts = {}
            if str(scripts.get("start") or "").strip():
                node_dirs.append(_dir_of(path))
        elif name.endswith(".py") and contents_known:
            # WITH THE SOURCE IN HAND, A NAME IS NOT THE TEST. A module that only defines things
            # is never the app (approvals.py, crun_3e370c11), and a server that lives inside its
            # package — lead_triage_core/server.py — is, even though it is not at the root.
            # A non-root file must both be a program and construct a server: that is what keeps
            # `myapp/server_helpers.py` from being declared on a prefix match.
            if _is_py_test_path(path):
                continue
            source = read(path) or ""
            if not python_source_is_program(source):
                continue
            if "/" not in path and (name in _PY_ENTRYPOINTS or name.startswith(("serve", "server"))):
                py_entries.append(path)
            elif python_source_serves(source):
                py_entries.append(path)
        elif "/" not in path and name.endswith(".py") and (
            name in _PY_ENTRYPOINTS or name.startswith(("serve", "server"))
        ):
            # ROOT LEVEL ONLY, matching resolve_app_entry's own scope. Without this, an ordinary
            # module like `myapp/server_helpers.py` reads as an entrypoint on a prefix match and
            # gets declared as the app — a wrong declaration is worse than none, because every
            # later preview trusts it instead of falling back to detection.
            py_entries.append(path)
        elif name == "index.html":
            static_dirs.append(_dir_of(path))

    for kind, found in (("node", node_dirs), ("py", py_entries), ("static", static_dirs)):
        unique = sorted(set(found))
        if not unique:
            continue
        # Two servers from one build is not a thing this can name. Stay silent rather than
        # commit a declaration that sends every future preview to the wrong one.
        if len(unique) > 1:
            return None
        return f"{kind}:{unique[0]}"
    return None


# The file in which a BUILD states how to run what it built: one shell command that starts
# the app and serves HTTP on $PORT. Equivalent: `"boss": {"preview": {"command": ...}}` in a
# package.json. The command runs from the directory holding the declaration.
RUN_DECLARATION_FILE = "boss.preview.command"


def _strip_cd_into_itself(directory: Path, command: str) -> str:
    """A declared command runs FROM the directory that declares it. One written for the
    repository root (`cd helpdesk-ai && npm …`, in helpdesk-ai/boss.preview.command) failed
    `can't cd to helpdesk-ai` and the app read as not serving (crun_cb6e15f4). A leading cd into
    the declaring directory itself, which does not exist inside it, is dropped."""
    import re as _re

    m = _re.match(r"\s*cd\s+(?:\./)?([\w.\-]+)/?\s*&&\s*(.+)$", command, _re.S)
    if m and m.group(1) == directory.name and not (directory / m.group(1)).exists():
        return m.group(2).strip()
    return command


def _declared_command_in(directory: Path) -> Optional[str]:
    """The run command declared in ONE directory, from either form, or None."""
    hint = directory / RUN_DECLARATION_FILE
    if hint.is_file():
        command = hint.read_text(errors="ignore").strip()
        if command:
            return _strip_cd_into_itself(directory, command)
    package = directory / "package.json"
    if package.is_file():
        try:
            payload = json.loads(package.read_text()) or {}
        except (OSError, ValueError):
            payload = {}
        configured = ((payload.get("boss") or {}).get("preview") or {}).get("command") \
            if isinstance(payload, dict) else None
        if isinstance(configured, str) and configured.strip():
            return _strip_cd_into_itself(directory, configured.strip())
    return None


def find_run_declaration(workdir: Path, changed: Optional[list[str]] = None
                         ) -> Optional[tuple[str, str]]:
    """(directory, command) the build DECLARED for running its app, or None.

    WHY THIS OUTRANKS EVERY RULE IN resolve_app_entry. That function has been patched once per
    incident — Sudoku for N-Queens (PR #62), the repo root for BugBox, a vite root with no
    entry, and lead_triage_core/approvals.py for the Lead Triage Inbox (crun_3e370c11) — because
    it infers at preview time a fact only the build knows. The implement stage is now required
    to write this declaration, and the build proves it serves (prove_app_serves) before the run
    can pass, so the preview runs a command that is already known to work.

    Only a declaration THIS lineage wrote counts when the lineage is known: a shared repo
    accumulates apps, and a declaration another app left at the root must not hijack this one.
    With no lineage at all (changed is None or empty) a root declaration is honoured, as it
    always was.
    """
    owned = [str(c).strip() for c in (changed or []) if str(c).strip()]
    for rel in owned:
        name = rel.rsplit("/", 1)[-1]
        if name not in (RUN_DECLARATION_FILE, "package.json"):
            continue
        rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else "."
        directory = workdir if rel_dir == "." else workdir / rel_dir
        command = _declared_command_in(directory)
        if command:
            return rel_dir, command
    if not owned:
        command = _declared_command_in(workdir)
        if command:
            return ".", command
    return None


_INSTALLS = re.compile(r"\b(npm\s+(install|ci|i)\b|pnpm\s+(install|i)\b|yarn(\s+install)?\s*(&&|$))")


def run_declared_command(rel_dir: str, command: str,
                         workdir: Optional[Path] = None) -> list[str]:
    """The argv for a declared command, run from the directory that declared it.

    PREPARED THE SAME WAY AS EVERY OTHER RUN. A declared command is proven by the build in the
    agent's workspace, where `node_modules` already exists, and then run by a preview from a
    fresh clone, where it does not. docs-freshness-board declared `npm run build && node
    server/dist/index.js`, passed its proof, and every preview of it died on "tsc: not found"
    (2026-10-03). A Node app's dependencies are installed first — with NPM_INSTALL_CMD, the one
    install BOSS runs everywhere — unless the declaration installs them itself.
    """
    base = workdir if rel_dir in ("", ".") or workdir is None else workdir / rel_dir
    if workdir is not None and (base / "package.json").is_file() and not _INSTALLS.search(command):
        command = f"{NPM_INSTALL_CMD} && {command}"
    if rel_dir in ("", "."):
        return ["sh", "-lc", command]
    return ["sh", "-lc", f"cd {shlex.quote(rel_dir)} && {command}"]


def parse_declared_app(text: str) -> Optional[tuple[str, str]]:
    """Read back what declare_app_entry wrote. Unknown kinds are ignored, not trusted."""
    kind, sep, value = (text or "").strip().partition(":")
    if not sep or kind not in {"node", "py", "static"} or not value.strip():
        return None
    return kind, value.strip()


class AmbiguousAppError(RuntimeError):
    """The repo holds several runnable apps and this task gave no signal which one is its own.

    Raised rather than guessed. The previous behaviour returned the first convention match,
    which showed an operator an application their task never touched — and in a repo shared
    across customers, another tenant's app entirely.
    """

    def __init__(self, candidates: list[str]) -> None:
        self.candidates = candidates
        super().__init__(
            "cannot tell which app this task built: the repository contains "
            f"{len(candidates)} runnable apps ({', '.join(candidates)}) and the run recorded "
            "no implementation files identifying one. Refusing to preview an arbitrary app. "
            "Retry the task so it records what it built, or add a boss.preview.command file."
        )


# The branch a run was built ON is the app it changes.
_WORK_BRANCH = re.compile(r"^boss/code/(crun_[a-z0-9]+)$", re.IGNORECASE)


def app_root_run_id(engine, run: dict) -> Optional[str]:
    """The run that ESTABLISHED this app, following every link BOSS records.

    Lineage was read from root_run_id and deferred_from_run only. Work started as its own task
    on an existing app's branch -- which is what "change this app" and every follow-up build
    does -- records neither: it records `spec.base_branch = boss/code/<run>`. So its file list
    was just the files that attempt happened to touch.

    For "Official bugbox - honest verify + JTBD e2e" that was the verification and e2e files.
    No index.html among them, so resolve_app_entry fell through to repo conventions and picked
    the repo ROOT, whose package.json has a `preview` script and no app to preview: rollup had
    no entry module to load and the preview timed out. The app it should have served --
    bugbox/, with its own index.html and server -- was never considered.

    The branch is the link. Walking it makes every attempt at an app resolve to the same app,
    for every stack, which is the property the three earlier fixes to this file were each
    reaching for one case at a time.
    """
    seen: set[str] = set()
    current = run
    for _ in range(10):                       # a cycle here would hang the preview
        fctx = current.get("finalize_ctx") or {}
        run_id = current.get("id")
        if run_id in seen:
            break
        if run_id:
            seen.add(run_id)
        root = fctx.get("root_run_id")
        if root and root != run_id:
            nxt = root
        else:
            spec = current.get("spec") or {}
            base = spec.get("base_branch") or ""
            matched = _WORK_BRANCH.match(str(base))
            nxt = matched.group(1) if matched else None
            if nxt == run_id:
                nxt = None
        if not nxt:
            return run_id
        with begin(engine) as c:
            row = c.execute(
                text("SELECT id, spec, finalize_ctx FROM code_task_runs WHERE id=:i"),
                {"i": nxt},
            ).mappings().first()
        if not row:
            # The branch names a run that no longer exists; this run is as far back as we go.
            return run_id
        current = dict(row)
    return current.get("id")


def lineage_impl_files(engine, run: dict) -> list[str]:
    """Every implementation file THIS TASK'S LINEAGE has written, newest attempt first.

    `state.impl_files` is per-ATTEMPT. On a repair it holds only the touched-up files — for
    crun_9b8e732b that was ["README.md", "nqueens_core/levels.py"], with no trace of the
    web-nqueens/ app the task exists to build. resolve_app_entry then finds no index.html
    among them and falls back to the repo-wide serve.py, so the preview served SUDOKU for an
    N-Queens task. Again.

    The branch holds the whole lineage's work, so the file list must too. (Deriving it with
    `git diff base...HEAD` is not an option: the preview clones --depth 1 --branch X, so the
    base ref does not exist in that workdir.)
    """
    fctx = run.get("finalize_ctx") or {}
    root = fctx.get("root_run_id")
    if not root and fctx.get("deferred_from_run"):
        # A deferred-findings FOLLOW-UP is spawned as its own root, so its file list is only the
        # findings it fixed — for crun_022620a9 that was ["nqueens_core/levels.py"], with no app
        # in it at all. It builds on the SAME branch as the run it came from, so its preview must
        # inherit that run's lineage. Resolved here rather than by writing root_run_id on the
        # follow-up, which would make the pipeline treat it as a retry and change its stages.
        with begin(engine) as c:
            parent = c.execute(
                text("SELECT COALESCE(finalize_ctx->>'root_run_id', id) AS r "
                     "FROM code_task_runs WHERE id=:i"),
                {"i": fctx["deferred_from_run"]},
            ).mappings().first()
        if parent:
            root = parent["r"]
    # Follow base_branch as well, so work started as its own task on an existing app's branch
    # inherits that app's files instead of only what this attempt touched.
    root = root or app_root_run_id(engine, run) or run.get("id")
    if not root:
        return list((run.get("state") or {}).get("impl_files") or [])
    with begin(engine) as c:
        rows = c.execute(
            text("""
                SELECT state->'impl_files' AS f
                FROM code_task_runs
                WHERE COALESCE(finalize_ctx->>'root_run_id', id) = :root
                   OR finalize_ctx->>'deferred_from_run' IN (
                        SELECT id FROM code_task_runs
                        WHERE COALESCE(finalize_ctx->>'root_run_id', id) = :root)
                ORDER BY created_at DESC
            """),
            {"root": root},
        ).mappings().all()
    out: list[str] = []
    for r in rows:
        for path in (r["f"] or []):
            if isinstance(path, str) and path not in out:
                out.append(path)
    return out


def _changed_dirs(changed: Optional[list[str]]) -> list[str]:
    """Directories this task touched, in the order they appear in the diff."""
    out: list[str] = []
    for c in changed or []:
        d = c.rsplit("/", 1)[0] if "/" in c else "."
        if d not in out:
            out.append(d)
    return out


# Conventional frontend folders of a full-stack app. Walking UP through these to find
# package.json `start` is correct (bugbox/web → bugbox). Walking from a sibling product
# directory (web-nqueens) up to an unrelated root start is how we once previewed the
# wrong tenant's application — so those stop before the repo root.
_FRONTEND_DIR_NAMES = frozenset({
    "web", "public", "dist", "src", "frontend", "client", "app", "ui", "static", "www",
})


def _has_start_script(directory: Path) -> bool:
    """True when directory/package.json declares a non-empty scripts.start."""
    manifest = directory / "package.json"
    if not manifest.is_file():
        return False
    try:
        scripts = json.loads(manifest.read_text()).get("scripts") or {}
    except (json.JSONDecodeError, OSError):
        return False
    return bool(str(scripts.get("start") or "").strip())


def _owning_node_server(workdir: Path, rel_dir: str) -> Optional[str]:
    """Nearest ancestor of rel_dir (inclusive) that declares scripts.start, or None.

    Used so a task that touched `bugbox/web/index.html` still runs `bugbox`'s Express
    server instead of `python3 -m http.server` on the SPA. A sibling static app
    (`web-nqueens/`) is not treated as owned by a root start.
    """
    parts: list[str] = [] if rel_dir in (".", "") else list(Path(rel_dir).parts)
    while True:
        candidate = workdir.joinpath(*parts) if parts else workdir
        if _has_start_script(candidate):
            return "." if not parts else str(Path(*parts))
        if not parts:
            return None
        if len(parts) == 1 and parts[0] not in _FRONTEND_DIR_NAMES:
            return None
        parts.pop()


def _node_server_dir(workdir: Path, changed: Optional[list[str]] = None) -> Optional[str]:
    """The directory of a package.json that declares a real `start` server, or None.

    A `start` script is the signal that something must be RUN, not served. Without this the
    resolver reached its static branch first -- and a Vite app has an `index.html` at its root,
    so an app with a working Express backend was classified `static:.` and published as raw
    source files. In the ApproveIt build the frontend loaded and every /api call 404'd, and the
    operator reasonably concluded the agent had built a shell.

    Subdirectories are checked before the root, and directories the task actually touched
    (including ancestors of `web/` / `public/`) before those it did not, so a repo holding
    several apps runs the one this task built.

    `dev`/`build` alone are deliberately NOT enough: a frontend-only Vite app has those and is
    correctly deployed as static output. Only `start` means "there is a server here".
    """
    seen: list[str] = []
    for touched in _changed_dirs(changed):
        owner = _owning_node_server(workdir, touched)
        if owner is not None and owner not in seen:
            seen.append(owner)
    for manifest in sorted(workdir.glob("*/package.json")):
        if _has_start_script(manifest.parent):
            rel = manifest.parent.name
            if rel not in seen:
                seen.append(rel)
    if _has_start_script(workdir) and "." not in seen:
        seen.append(".")
    # One unambiguous server wins. Several means the same ambiguity the static path refuses,
    # and guessing is what once previewed another tenant's application.
    return seen[0] if seen else None


def resolve_app_entry(workdir: Path, changed: Optional[list[str]] = None) -> tuple[str, str]:
    """THE single decision about which app a preview launches: ("py"|"static"|"node"|"", value).

    Both the host path (_detect_command) and the container path (_generate_dockerfile) call
    THIS. They previously each carried their own copy of the preference logic, and the copies
    drifted twice:

      PR #62  — the host path learned to prefer the entrypoint the task built; the container
                path did not, and previewed Sudoku for an N-Queens build.
      crun_9b8e732b — both paths then preferred a CHANGED .py entrypoint, but neither applied
                the same preference to STATIC dirs. _STATIC_DIRS tries "." first, and a task
                that built `web-nqueens/index.html` in a repo whose root already held the
                Sudoku app previewed Sudoku again — this time via the repo-wide serve.py.

    Hence one function. "What this task built" outranks "what the repo happens to contain",
    for every stack, and there is exactly one place to change that rule.
    """
    touched = _changed_dirs(changed)

    # 1. An entrypoint THIS task wrote — a PROGRAM, not a name.
    #
    # This rule took any changed .py whose basename began with serve/server/app/main/wsgi, so
    # lead_triage_core/approvals.py ("app…") was chosen over the server the same build wrote,
    # run as a script, and died on its own relative imports (crun_3e370c11). A candidate must
    # now be a program (python_source_is_program). A name-matched program wins first, as
    # before; a program that constructs a server is the app whatever it is called. Package
    # modules are run with -m by command_for_entry, so choosing one here is safe.
    #
    # Among the qualified, what the file DOES ranks before what it is called and where it sits
    # in the changed list: a server beats a __main__ script beats a module that merely makes a
    # top-level call. Otherwise app_settings.py calling load_dotenv() is "a program" named like
    # an app, and outranks the server.py written beside it.
    ranked = []
    for order, cand in enumerate(
            [c for c in (changed or []) if c.endswith(".py") and not _is_py_test_path(c)]):
        source = _read_py(workdir, cand)
        if source is None or not python_source_is_program(source):
            continue
        named = cand.rsplit("/", 1)[-1].startswith(_PY_ENTRY_PREFIXES)
        strength = python_entry_strength(source)
        if named or strength == 0:
            ranked.append((strength, 0 if named else 1, order, cand))
    if ranked:
        return ("py", min(ranked)[3])
    # 2. A static app THIS task built. Ranked ABOVE the repo-wide python fallback: a task that
    #    ships web-nqueens/index.html means that app, not whatever serve.py predates it.
    #
    #    A start server that OWNS that index.html outranks the static file. BugBox/ApproveIt
    #    lineage lists `bugbox/web/index.html`; returning static here previewed the SPA and
    #    every /api call came back as the Vite 404 HTML page. The owning check walks ancestors
    #    (bugbox/web → bugbox) and refuses to let a root start steal a sibling product dir.
    for d in touched:
        if (workdir / d / "index.html").is_file():
            owner = _owning_node_server(workdir, d)
            if owner is not None:
                return ("node", owner)
            return ("static", d)
    # 3. Repo conventions — but ONLY when the repo is unambiguous.
    #
    # FAIL CLOSED ON AMBIGUITY. This used to walk the conventions and return the first hit, so
    # a task whose own output revealed no app previewed whatever the repo happened to contain.
    # A deferred-findings follow-up touching only nqueens_core/levels.py showed the operator
    # the SUDOKU app sitting at the repo root. In a repo holding one customer's work that is a
    # confusing preview; in a repo holding several it is showing someone another tenant's
    # application. Serving the wrong app is strictly worse than serving nothing, so when the
    # repo offers more than one candidate and the task gives no signal, we refuse and say why.
    # A CANDIDATE MUST BE ABLE TO RUN.
    #
    # boss-sandbox's ROOT package.json carries `"preview": "vite preview"` and the root holds
    # no index.html and no src/ -- it is a monorepo root, not an app. Anything that selected it
    # produced `rollup ... loadEntryModule` and a preview that never served, reported to the
    # operator as "app did not serve HTTP within timeout": a timeout for a build that could
    # never have succeeded. A directory whose node scripts have nothing to build is not a
    # candidate, whatever else the conventions say about it.
    def _node_dir_can_build(rel: str) -> bool:
        base = workdir / rel if rel != "." else workdir
        manifest = base / "package.json"
        if not manifest.is_file():
            return False
        try:
            scripts = (json.loads(manifest.read_text()) or {}).get("scripts") or {}
        except (ValueError, OSError):
            return False
        # A server entry can serve without an index.html; a bundler cannot.
        if scripts.get("start"):
            return True
        if not any(k in scripts for k in ("dev", "build", "preview")):
            return False
        return (base / "index.html").is_file()

    candidates: list[tuple[str, str]] = []

    def _root_program(name: str) -> bool:
        # Same rule as step 1: a module of definitions named app_utils.py is not an app, and
        # counting it made a repo with one real server look ambiguous (or chose the module).
        source = _read_py(workdir, name)
        return source is not None and python_source_is_program(source)

    for entry in _PY_ENTRYPOINTS:
        if (workdir / entry).exists() and _root_program(entry):
            candidates.append(("py", entry))
    for cand in sorted(workdir.glob("*.py")):
        if cand.name.startswith(_PY_ENTRY_PREFIXES) and _root_program(cand.name):
            if ("py", cand.name) not in candidates:
                candidates.append(("py", cand.name))
    # A runnable server outranks its own static assets: serving an app's index.html without
    # its API is a page that loads and does nothing.
    server_dir = _node_server_dir(workdir, changed)
    if server_dir is not None and _node_dir_can_build(server_dir):
        return ("node", server_dir)
    for rel in _STATIC_DIRS:
        if (workdir / rel / "index.html").is_file():
            candidates.append(("static", rel))
    # Any OTHER top-level directory holding an index.html is a candidate too. Counting only the
    # convention names made a multi-app repo look unambiguous: boss-sandbox holds web/ (Sudoku),
    # web-stock/ and web-nqueens/, but only "web" is a convention, so a task with no signal got
    # a confident answer of SUDOKU rather than the refusal this function promises. Ambiguity has
    # to be measured against what the repo actually contains.
    for cand in sorted(workdir.glob("*/index.html")):
        rel = cand.parent.name
        if ("static", rel) not in candidates:
            candidates.append(("static", rel))
    # Last-resort root node app -- but only if the root can actually build. A monorepo root
    # whose package.json exists purely to hold a `preview` script is not an app, and choosing
    # it produced a preview that could never serve.
    if not candidates and (workdir / "package.json").exists() and _node_dir_can_build("."):
        candidates.append(("node", "."))
    # A Java service (pom.xml / build.gradle) in THE APP'S directory: build the jar and run it.
    # The root only when the app lives there (app_root): a shared repository's root pom.xml is
    # another app's, never the one this lineage built in its own folder.
    if not candidates:
        from . import app_root

        where, build = app_root.java_build(workdir, changed=changed)
        if build:
            rel = where.relative_to(workdir).as_posix() if where != workdir else "."
            return ("java", rel)
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise AmbiguousAppError([f"{k}:{v}" for k, v in candidates])
    return ("", "")


class AppRun:
    """What runs a lineage's app: the argv, where that answer came from, and the app it names.

    `source` is "declared" (the build's boss.preview.command), "derived" (the app the build
    recorded in boss.preview.app) or "resolved" (the preview resolver's inference). `entry` is
    the (kind, value) of the app when one is known; a declared command names no entry.
    """

    __slots__ = ("command", "source", "entry", "declared_in", "declared_command")

    def __init__(self, command: Optional[list[str]], source: str,
                 entry: Optional[tuple[str, str]] = None,
                 declared_in: Optional[str] = None,
                 declared_command: Optional[str] = None) -> None:
        self.command = command
        self.source = source
        self.entry = entry
        # For a declared command: the directory that declared it and the command as written,
        # which runs FROM that directory.
        self.declared_in = declared_in
        self.declared_command = declared_command

    def shell(self) -> str:
        """The command as ONE shell string, for runners that take a string (a container CMD)."""
        argv = self.command or []
        if argv[:2] == ["sh", "-lc"] and len(argv) == 3:
            return argv[2]
        return shlex.join(argv)


def how_app_runs(workdir: Path, changed: Optional[list[str]] = None) -> Optional[AppRun]:
    """THE answer to "what command runs this lineage's app". Every runner asks this function.

    WHY ONE FUNCTION. The serve proof (prove_app_serves) asked find_run_declaration about this
    run's diff alone while the preview asked it about the whole lineage (lineage_impl_files). A
    follow-up build of a declared FastAPI app — its boss.preview.command written by the run
    before — never saw the declaration, fell back to heuristics, proved `python3 main.py`, and
    failed; the preview would have run the declared uvicorn command and worked (second review
    of f16b5d4). And the deploy ran `python {entry}`, ignoring both. Three runners, three
    answers. Now the preview, the proof and the container preview all call this with the
    lineage's files, so "it served when the build checked" is a claim about the command the
    preview later runs. The deploy deliberately does NOT: a declaration is a preview and
    build-proof contract, and production keeps its own commands (npm start; python3 -m for a
    package module) — running a declared preview command in a production container broke
    apps that needed devDependencies (fourth review).

    Raises AmbiguousAppError rather than guessing (see _detect_command's history).
    """
    # HOW THE BUILD SAID TO RUN IT beats everything, including which app BOSS derived: the
    # build wrote the command and proved it served before it passed (find_run_declaration).
    run_declaration = find_run_declaration(workdir, changed)
    if run_declaration:
        return AppRun(run_declared_command(*run_declaration, workdir=workdir), "declared",
                      declared_in=run_declaration[0], declared_command=run_declaration[1])

    # WHAT THE BUILD DECLARED BEATS ANYTHING INFERRED LATER. The build knew which app it
    # produced; everything below is this function trying to work that out again from a
    # repository that has gained apps since. Read the declaration first and skip the guessing.
    #
    # The same ownership rule as the run declaration: when the lineage is known, a
    # boss.preview.app naming an app it never wrote is another app's (a root file merged in
    # from elsewhere) and must not decide what this lineage runs — least of all at deploy,
    # where an operator's app_entry hint rides in `changed` and the wrong app is served
    # publicly.
    declared = workdir / DECLARED_APP_FILE
    if declared.is_file():
        entry = parse_declared_app(declared.read_text(errors="ignore"))
        owned = [_repo_rel(c) for c in (changed or []) if str(c).strip()]
        if entry and owned and not _entry_is_the_lineages(entry, owned):
            entry = None
        if entry:
            command = command_for_entry(workdir, entry[0], entry[1])
            if command:
                return AppRun(command, "derived", entry)
            logger.warning(
                "preview: build declared %s:%s but no command could be built for it — "
                "falling back to detection", *entry)

    app_rel = "."
    # REFUSE, DO NOT FALL BACK TO THE ROOT: an AmbiguousAppError propagates. Swallowing it
    # turned a precise, actionable refusal ("the repository contains 9 runnable apps and the
    # run recorded no implementation files identifying one") into a silent guess at the repo
    # root — the directory resolution had just declined to choose. Callers record it as
    # failure_kind 'ambiguous_app'. Fail closed and say why.
    kind, value = resolve_app_entry(workdir, changed)
    entry = (kind, value) if kind and value else None
    # A RESOLVED PYTHON APP IS THE ANSWER, NOT A REASON TO GO READ THE ROOT MANIFEST.
    #
    # Only node/static were honoured here, so a task whose app is a Python service fell
    # through to _detect_command_in(root) — and in a repo whose root package.json declares
    # vite, the Vite branch fired before anything asked about Python. The interview-scorecard
    # run built scorecard_core/ + serve_scorecard.py, resolution answered
    # `py:serve_scorecard.py` correctly, and the preview ran `npx vite build` at the
    # workspaces root: "Build failed in 6ms | Could not resolve entry module index.html",
    # nothing listened, "app did not serve HTTP within timeout" — for an app that was never
    # started.
    if kind in ("py", "java") and value:
        command = command_for_entry(workdir, kind, value)
        return AppRun(command, "resolved", entry) if command else None
    if kind in {"node", "static"} and value not in {"", "."}:
        app_rel = value
    command = _detect_command_in(workdir if app_rel == "." else workdir / app_rel, changed)
    if command and app_rel != "." and command[:2] == ["sh", "-lc"]:
        # Run it where the app is. `npm install` in the wrong directory installs the wrong
        # dependency tree, which is the other half of why this failed the way it did.
        command = ["sh", "-lc", f"cd {shlex.quote(app_rel)} && {command[2]}"]
    return AppRun(command, "resolved", entry) if command else None


def _detect_command(workdir: Path, changed: Optional[list[str]] = None) -> Optional[list[str]]:
    """Build the run command FOR THE APP resolve_app_entry chose — via how_app_runs.

    THE BUG THIS FIXED. resolve_app_entry is the single decision about which app a preview
    launches, and this function ignored it: it read `workdir/package.json` -- the repo ROOT --
    and built a command from that. In boss-sandbox the root is a monorepo root whose
    devDependencies contain `vite`, so the Vite branch fired and ran `npx vite build` at a
    directory with no index.html and no src. Rollup had nothing to load
    ("ModuleLoader.loadEntryModule"), nothing ever listened, and the operator was told "app did
    not serve HTTP within timeout" -- three times, while resolution was correct all along and
    answered `node:bugbox` every time. The decision now lives in how_app_runs, which every
    runner shares.
    """
    plan = how_app_runs(workdir, changed)
    if plan is None:
        return None
    logger.info("preview: running the %s command %s", plan.source, plan.command)
    return plan.command


def _detect_command_in(workdir: Path, changed: Optional[list[str]] = None) -> Optional[list[str]]:
    """Detect the declared preview command for ONE app directory."""
    configured = _declared_command_in(workdir)
    if configured:
        return ["sh", "-lc", configured]
    package = workdir / "package.json"
    if package.is_file():
        try:
            payload = json.loads(package.read_text())
        except (OSError, json.JSONDecodeError):
            payload = {}
        scripts = payload.get("scripts") or {}
        deps = {**(payload.get("dependencies") or {}), **(payload.get("devDependencies") or {})}
        is_vite = ("vite" in deps or (workdir / "vite.config.ts").exists()
                   or (workdir / "vite.config.js").exists())
        # A DEPENDENCY IS NOT AN APPLICATION.
        #
        # `vite` in package.json says the toolchain is available, not that anything was built
        # with it. A task seeded with the full-stack scaffold and then written as a Python
        # service ships exactly that: the scaffold's package.json, and no index.html anywhere.
        # `vite build` then fails in 11ms with "Could not resolve entry module index.html",
        # the preview times out waiting for a port, and the operator is told to retry a task
        # that completed perfectly well and opened PR #161.
        #
        # Vite cannot build without an entry, so if there is no index.html to be found this is
        # not a Vite app whatever the manifest claims — fall through to the other detectors,
        # which can find the Python or Node server that IS there.
        if is_vite and not any((workdir / d / "index.html").is_file() for d in _STATIC_DIRS):
            logger.info(
                "preview: package.json declares vite but no index.html exists under %s — "
                "not treating this as a Vite app",
                ", ".join(_STATIC_DIRS),
            )
            is_vite = False
        # A Vite app: BUILD it and serve the STATIC output — do NOT run the dev server. The dev
        # server emits origin-absolute ES-module imports (/node_modules/.vite/deps, /src, /@vite)
        # that the browser resolves against the API origin root, NOT the /v1/previews/<id>/web
        # proxy prefix, so every module 404s and the page renders blank. A production build bundles
        # to RELATIVE-path assets (base=./) that resolve correctly under any proxy prefix via the
        # injected <base href>. Served by Python's stdlib http.server (guaranteed present).
        if is_vite and (scripts.get("build") or "vite" in deps):
            # A Vite app WITH a server of its own: build the frontend, then run that server.
            #
            # This branch used to fire on `vite` alone and serve dist/ with http.server, which
            # is right for a frontend-only app and silently wrong for a full-stack one: the
            # page loads, looks finished, and every /api call returns 404. BugBox shipped a
            # working Express + SQLite backend and previewed as "Error: HTTP 404" because
            # nothing was running it.
            #
            # How it is BUILT is node_build_step's one decision: the app's own build, with
            # base=./ added for a Vite entry it has found (absolute /assets/... 404 under the
            # /v1/previews/<id>/web prefix, and the injected <base href> cannot rescue them).
            build = node_build_step(workdir, payload)
            if scripts.get("start"):
                return [
                    "sh", "-lc",
                    f"{NPM_INSTALL_CMD} && "
                    f"{build}npm start",
                ]
            return [
                "sh", "-lc",
                f"{NPM_INSTALL_CMD} && "
                f"{build}"
                'python3 -m http.server "$PORT" --bind 0.0.0.0 --directory dist',
            ]
        script = "dev" if scripts.get("dev") else "start" if scripts.get("start") else None
        if script:
            suffix = " -- --host 0.0.0.0" if script == "dev" else ""
            # `start` RUNS the app; it does not compile it. A TypeScript service whose start
            # script is `node dist/server.js` needs `dist/` to exist first, and this branch went
            # straight from install to start — so the app died on MODULE_NOT_FOUND and the
            # operator watched "Starting preview…" for minutes before it timed out. Shorty, a
            # complete and correct URL shortener, could not be previewed for exactly this reason.
            #
            # The build was already run on the two Vite branches above and in the Dockerfile
            # path; this branch and no other was missed, which is the whole shape — the same
            # requirement expressed in four places and satisfied in three.
            #
            # `dev` is excluded deliberately: a dev server compiles on the fly, and building
            # first would only slow it down. `--if-present` makes this a no-op for an app that
            # has no build script, so nothing that worked before changes.
            build = "" if script == "dev" else "npm run build --if-present && "
            return [
                "sh", "-lc",
                f"{NPM_INSTALL_CMD} && "
                f"{build}npm run {script}{suffix}",
            ]
    procfile = workdir / "Procfile"
    if procfile.is_file():
        for line in procfile.read_text(errors="ignore").splitlines():
            kind, sep, command = line.partition(":")
            if sep and kind.strip().lower() == "web" and command.strip():
                return ["sh", "-lc", command.strip()]
    makefile = workdir / "Makefile"
    if makefile.is_file():
        content = makefile.read_text(errors="ignore")
        for target in ("dev", "run"):
            if re.search(rf"(?m)^{target}\s*:", content):
                return ["make", target]
    # PREFER WHAT THIS TASK BUILT. Selection used to walk a fixed filename list over the whole
    # repo, so a task that added `serve_nqueens.py` to a repo already containing the Sudoku
    # `serve.py` previewed SUDOKU — serve.py sorts earlier and serve_nqueens.py was not on the
    # list at all. The preview faithfully launched the wrong application (PR #62).
    # ONE decision, shared with the container path (see resolve_app_entry).
    kind, value = resolve_app_entry(workdir, changed)
    return command_for_entry(workdir, kind, value)


_VITE_CONFIG_NAMES = ("vite.config.ts", "vite.config.js", "vite.config.mts", "vite.config.mjs")


def node_build_step(app_root: Path, manifest: dict) -> str:
    """The shell fragment that BUILDS a Node app for preview: "" or a command ending in `&& `.

    THE APP'S OWN BUILD IS THE ONLY BUILD. Both Node paths used to replace it with BOSS's guess,
    `npx vite build --base=./` at the app root, whenever `vite` was a dependency.
    docs-freshness-board (boss-sandbox PR #219) keeps its frontend in web/ and builds with
    `tsc -p server/... && vite build --config web/vite.config.ts`; the guess ran vite where there
    is no index.html ("Could not resolve entry module", 10ms) and skipped the server compile
    `npm start` needs. Adding a rebuild AFTER the app's build is the same mistake smaller: it
    empties the outDir the app's build just wrote (tsc output, prerendered pages).

    BOSS adds only what the preview proxy needs — RELATIVE asset URLs, because absolute
    /assets/... 404 under the /v1/previews/<id>/web/ prefix — and only where it is the app's own
    command that receives it: a vite app whose build script ENDS in `vite build` gets
    `npm run build -- --base=./` (npm appends the flag to that last command). A vite app with no
    build script at all, and its index.html at the root, gets `npx vite build --base=./`.
    Anything else is built exactly as the app builds itself.
    """
    scripts = manifest.get("scripts") or {}
    deps = {**(manifest.get("dependencies") or {}), **(manifest.get("devDependencies") or {})}
    uses_vite = "vite" in deps or any((app_root / name).is_file() for name in _VITE_CONFIG_NAMES)
    build_script = str(scripts.get("build") or "").strip()
    if build_script:
        last = re.split(r"&&|\|\||;", build_script)[-1]
        if uses_vite and re.search(r"(^|\s)vite\s+build\b", last):
            return "npm run build -- --base=./ && "
        return "npm run build && "
    if uses_vite and (app_root / "index.html").is_file():
        return "npx vite build --base=./ && "
    return ""


def command_for_entry(workdir: Path, kind: str, value: str) -> Optional[list[str]]:
    """The command that runs ONE resolved app entry. The only place an entry becomes a command.

    Extracted so the app BOSS declared at build time and the app a heuristic resolved at
    preview time go through identical construction. Two copies of this is the recurring bug in
    this file, not a hypothetical: `_detect_command` once built its own command from the repo
    root while `resolve_app_entry` was answering correctly, and every symptom of that — vite
    building a directory with no entry, a preview launching another task's app — was a second
    copy of this logic disagreeing with the first.
    """
    if kind == "node":
        # Run the app's OWN server, from its OWN directory, so `npm install` and the start
        # script resolve that app's manifest rather than the repository root's. The root check
        # above only ever looked at `workdir/package.json`, so an app built in a subdirectory
        # fell through to the static branch and was published without its backend.
        app_dir = "" if value in (".", "") else f"cd {shlex.quote(value)} && "
        app_root = workdir if value in (".", "") else workdir / value
        try:
            manifest = json.loads((app_root / "package.json").read_text())
        except (OSError, json.JSONDecodeError):
            manifest = {}
        build = node_build_step(app_root, manifest if isinstance(manifest, dict) else {})
        return [
            "sh", "-lc",
            f"{app_dir}{NPM_INSTALL_CMD} && "
            f"{build}npm start",
        ]
    if kind == "py":
        # python_entry_invocation decides script vs `-m` — a package module run as a script
        # cannot resolve its relative imports (crun_3e370c11).
        run_dir, argv = python_entry_invocation(workdir, value)
        if run_dir == ".":
            return argv
        return ["sh", "-lc", f"cd {shlex.quote(run_dir)} && {shlex.join(argv)}"]
    if kind == "java":
        from . import toolchains

        app_root = workdir if value in (".", "") else workdir / value
        build = toolchains.detect_java(app_root)
        if build is None:
            return None
        if not toolchains.has("java"):
            return ["sh", "-lc", "echo " + shlex.quote(toolchains.missing_message(
                "java", toolchains.build_file(build))) + " >&2; exit 78"]
        argv = toolchains.java_preview_command(app_root, build)
        return argv if value in (".", "") else ["sh", "-lc", f"cd {shlex.quote(value)} && {argv[2]}"]
    if kind == "static":
        # A static site still needs a SERVER — ES modules refuse to load over file:// — so a
        # bare index.html is a runnable app, not a library.
        return ["sh", "-lc",
                f'python3 -m http.server "$PORT" --bind 0.0.0.0 --directory {value}']
    return None


def _detect_verification(workdir: Path) -> Optional[str]:
    """A library/CLI/module build has no server to preview — the way you 'run' it is its test
    suite. Detect that so the preview can report an honest 'verified by tests' result instead of a
    misleading 'you forgot to add a preview command' error. Returns a human-readable test command."""
    package = workdir / "package.json"
    if package.is_file():
        try:
            scripts = (json.loads(package.read_text()).get("scripts") or {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        if scripts.get("test"):
            return "npm test"
    makefile = workdir / "Makefile"
    if makefile.is_file() and re.search(r"(?m)^test\s*:", makefile.read_text(errors="ignore")):
        return "make test"
    has_py_tests = (
        any(workdir.glob("test_*.py")) or any(workdir.glob("*_test.py"))
        or any(workdir.glob("**/test_*.py")) or (workdir / "tests").is_dir()
    )
    if has_py_tests:
        if (workdir / "pytest.ini").is_file() or (workdir / "pyproject.toml").is_file():
            return "python3 -m pytest -q"
        return "python3 -m unittest discover -v"
    return None


def _descendant_pids(pid: int) -> list[int]:
    pids = [pid]
    try:
        out = owned_process.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5, sandboxed=False).stdout
        for child in out.split():
            pids.extend(_descendant_pids(int(child)))
    except Exception:
        pass
    return pids


def _url_ok(url: str, *, headers: Optional[dict[str, str]] = None, check_body: bool = False) -> bool:
    try:
        request = urllib.request.Request(url, headers=headers or {})
        response = urllib.request.urlopen(request, timeout=2)
        status = int(response.status)
        if not (200 <= status < 400):
            return False
        # APP-4 (#94): the primary-route smoke must see real served content, not just a socket. A
        # blank body or a rendered error overlay on a 2xx no longer counts as healthy. (An SPA shell
        # is non-blank and not an error page, so it still passes — full render rubric is the headless gate.)
        if check_body and 200 <= status < 300:
            body = response.read(65536).decode("utf-8", "ignore")
            if not body.strip():
                return False
            from .visual_gate import is_error_page
            if is_error_page(body):
                return False
        return True
    except urllib.error.HTTPError:
        # A framework error page is not a healthy preview.
        return False
    except Exception:
        return False


HEALTH_DECLARATION_FILE = "boss.preview.health"


def preview_health_path(workdir: Optional[Path]) -> str:
    """The path a preview's health check asks for: the one line of boss.preview.health, else the
    health check the app's own boss.deploy.yaml declares, else `/`.

    ONE DECLARATION OF HEALTH, NOT TWO. An API with nothing at `/` (a JSON order service, a
    Dropwizard `/hello`) declares `health: [{url: http://127.0.0.1:8000/health}]` for its deploy
    and was then failed by its preview as "no page at the site root — check that the build
    produced a UI and not only an API", because the preview only read its own file. The deploy
    manifest is the app saying where it is healthy; the preview asks the same path."""
    try:
        line = (Path(workdir) / HEALTH_DECLARATION_FILE).read_text(errors="ignore").strip()
    except (OSError, TypeError):
        line = ""
    line = line.splitlines()[0].strip() if line else ""
    if line.startswith("/") and "://" not in line and len(line) < 300:
        return line
    return _declared_deploy_health_path(workdir) or "/"


def _declared_deploy_health_path(workdir: Optional[Path]) -> Optional[str]:
    """The path of the first health check in the app's boss.deploy.yaml, or None."""
    from urllib.parse import urlsplit

    from .app_deploy_manifest import DEFAULT_MANIFEST_PATH, ManifestError, load_manifest

    if workdir is None:
        return None
    try:
        manifest = load_manifest(Path(workdir) / DEFAULT_MANIFEST_PATH)
    except (OSError, TypeError, ValueError, ManifestError):
        return None
    for check in manifest.health:
        path = urlsplit(str(check.url or "")).path
        if path.startswith("/") and len(path) < 300 and "{" not in path:
            return path
    return None


def preview_start_timeout(workdir: Optional[Path], default: float = 180.0, *,
                          changed: Optional[list[str]] = None,
                          limit: Optional[float] = None,
                          command: Optional[list[str]] = None) -> float:
    """A Java service builds its jar (and downloads its dependencies) before it binds. `workdir`
    is the app's directory, or a checkout whose app app_root finds from `changed`.

    PASS `changed` FOR A SHARED REPOSITORY. Without it the app is placed from the checkout alone,
    and a pom.xml at the root of a repository that holds a Node app among others made that Node
    app "Java": its preview waited 600 s instead of 180 s (crun_16c5e245, 2026-10-18). That
    repository has its package.json at the root beside the pom.xml, so even `changed` places the
    app at the root: given the `command` that will start it, the Java window applies only when
    that command builds or runs Java. `limit` caps the answer for a caller that must not wait
    that long whatever the app is."""
    from . import app_root

    try:
        seconds = (max(default, 600.0)
                   if workdir and (command is None or _command_is_java(command))
                   and app_root.java_build(workdir, changed=changed)[1]
                   else default)
    except OSError:
        seconds = default
    return min(seconds, limit) if limit else seconds


_JAVA_LAUNCHERS = frozenset({"mvn", "mvnw", "gradle", "gradlew", "java", "jbang"})


def _command_is_java(command) -> bool:
    """Does this start command build or run Java (`./mvnw spring-boot:run`, `java -jar …`)?"""
    words = command if isinstance(command, (list, tuple)) else [str(command or "")]
    for word in re.split(r"[\s;&|()]+", " ".join(str(w) for w in words)):
        if word.rsplit("/", 1)[-1] in _JAVA_LAUNCHERS:
            return True
    return False


def _http_ok(port: int, path: str = "/") -> bool:
    return _url_ok(f"http://127.0.0.1:{port}{path if path != '/' else ''}", check_body=True)


#: Statuses that say the ROUTE is not there, as opposed to "not ready yet". An app answering
#: one has bound and is routing requests; waiting longer does not grow it a route.
ABSENT_ROUTE_STATUSES = (404, 410)

#: How long a port may keep answering ABSENT_ROUTE_STATUSES at the health path before discovery
#: calls it a failure. A short grace, not zero, for a server that binds a moment before it
#: mounts its routes — but seconds, never the start window: the Onboarding Path Builder's
#: preview answered 404 at `/` from its first second and was polled for 600 s, holding its
#: build's job (and the build's follow-ups) the whole time.
ABSENT_ROUTE_GRACE_SECONDS = 15.0


def _route_absent(port: int, path: str = "/") -> Optional[int]:
    """The status when `path` on `port` answers one of ABSENT_ROUTE_STATUSES, else None."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=2)
        try:
            conn.request("GET", path or "/")
            status = int(conn.getresponse().status)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — nothing answering is "not ready", not "absent"
        return None
    return status if status in ABSENT_ROUTE_STATUSES else None


def _diagnose_listening_port(log_path: str, port_hint: int,
                             health_path: str = "/") -> Optional[str]:
    """Why a port that IS listening still failed the health check.

    "app did not serve HTTP within timeout" describes two opposite situations and distinguishes
    neither: nothing ever bound (the build failed), or the app bound fine and answered the probe
    with something _http_ok rejects. The interview-scorecard app was the second — it serves
    /api/* and /metrics and has no route at all for "/", so the probe's 404 read as dead. The
    log said "Serving the interview scorecard app on http://0.0.0.0:47053" and the operator was
    told it never served, which sent everyone looking at the wrong half of the system.

    Returns a sentence naming the port and what the health path (`/` unless the app declares
    one — preview_health_path) actually answered, or None when nothing is listening (in which
    case the generic timeout really is the right description). It asks the SAME path the
    readiness check asked: a JSON API declaring `/health` that 404s there was reported as
    "GET / returned 404", a path nobody had asked about.
    """
    path = health_path if (health_path or "").startswith("/") else "/"
    for port in _candidate_ports(log_path, port_hint):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1.0)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                continue
        try:
            response = urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path if path != '/' else ''}", timeout=2)
            status, body = int(response.status), response.read(4096)
            if not body.strip():
                return (f"the app IS listening on port {port}, but GET {path} returned {status} "
                        f"with an empty body, so there is no page to preview.")
            return (f"the app IS listening on port {port} and GET {path} returned {status}, but "
                    f"the health check rejected the page it served.")
        except urllib.error.HTTPError as http_error:
            if path != "/":
                return (f"the app IS listening on port {port}, but GET {path} returned "
                        f"{http_error.code}. It serves no health route at the path it declares "
                        f"(boss.deploy.yaml health / boss.preview.health) — fix the route or the "
                        f"declaration.")
            return (f"the app IS listening on port {port}, but GET / returned "
                    f"{http_error.code}. It serves no page at the site root — check that the "
                    f"build produced a UI and not only an API.")
        except Exception:  # noqa: BLE001 — a port that will not answer is a port that failed
            return (f"the app IS listening on port {port} but GET {path} did not complete.")
    return None


#: What the preview checker tells the operator to do, per failure kind. One table, so anything
#: that must recognise the checker's OWN words — rather than the failure's — can read them all:
#: `turn_subject.cause_marks` refuses to count an answer that only repeats this advice as one
#: that used the recorded failure (the port_timeout misdiagnosis was exactly that).
START_FAILURE_ADVICE: dict[str, str] = {
    # BOSSAPP-860: the app ran, but its own HTTPS origin (BOSS_PREVIEW_EXPOSE) was not verified.
    "expose_failed": ("The app started, but BOSS could not give it its own HTTPS origin and "
                      "verify it from outside: check the boss-expose helper and its backend "
                      "(tailscale serve or Caddy), or turn BOSS_PREVIEW_EXPOSE off."),
    "no_page_at_root": ("The app started but serves nothing at '/'. Check that the build "
                        "produced a UI and not only an API, then preview it again."),
    "no_health_route": ("The app started but its declared health path answered 404. Fix the "
                        "route, or the health path in boss.deploy.yaml / boss.preview.health, "
                        "then preview it again."),
    "clone_failed": "Check repository access and the task branch, then retry.",
    "command_missing": "Add boss.preview.command or retry with an explicit command.",
    "port_timeout": ("Inspect startup logs, ensure HOST/PORT are honored, or retry in Docker "
                     "mode."),
    "boot_crash": ("The app crashed while starting — read the error in its log (an import or "
                   "syntax error is a code fix, not a port), fix it, then preview again."),
}

#: The checker's own wrapper sentences around a failure, as they appear in a step's error.
CHECKER_LABELS: tuple[str, ...] = (
    "app did not serve HTTP within timeout",
    "container did not serve HTTP",
    "the preview it started finished as",
    "Traceback (most recent call last)",
)


#: What a CRASH looks like in an app's own output, as opposed to an app that is slow to bind.
#: Lower-case; matched against the failure message, which carries the log's error lines.
CRASH_MARKERS: tuple[str, ...] = (
    "traceback (most recent call last)",
    "importerror",
    "modulenotfounderror",
    "syntaxerror",
    "cannot find module",
    "err_module_not_found",
    "the app exited",
    "segmentation fault",
    "panic:",
)


def _classify_start_failure(message: str, *, exited: Optional[bool] = None) -> tuple[str, str]:
    lowered = message.lower()
    if lowered.startswith("expose:"):
        return "expose_failed", START_FAILURE_ADVICE["expose_failed"]
    # AN APP THAT IS LISTENING DID NOT TIME OUT. Checked before the generic "port" rule below,
    # which matches the word in this message too and labelled it port_timeout — telling the
    # operator to "ensure HOST/PORT are honored" about an app that honoured both and bound
    # exactly as asked. The fix here is a UI, not a port.
    if "no page at the site root" in lowered:
        kind = "no_page_at_root"
    elif "no health route at the path it declares" in lowered:
        kind = "no_health_route"
    elif "clone" in lowered:
        kind = "clone_failed"
    # A CRASH IS A CRASH, whatever wrapper sentence it arrives in. boss-1, 2026-10-02: a
    # Preview step died on an ImportError at import time; the message said "app did not serve
    # HTTP within timeout … ImportError …", the "did not serve" rule below won, and the row was
    # labelled port_timeout with "ensure HOST/PORT are honored" — sending the operator, and the
    # agent asked about it, after a port that was never the problem. A process that exited, or
    # a log that shows a traceback, is checked BEFORE the timeout rule.
    elif exited or any(marker in lowered for marker in CRASH_MARKERS):
        kind = "boot_crash"
    elif "detect" in lowered or "command" in lowered:
        kind = "command_missing"
    elif "did not serve" in lowered or "port" in lowered:
        kind = "port_timeout"
    else:
        kind = "boot_crash"
    return kind, START_FAILURE_ADVICE[kind]


def _candidate_ports(log_path: str, port_hint: int) -> list[int]:
    """Ordered candidates that unambiguously belong to THIS app:
    1) the URL/port the framework printed in its own log, then
    2) the PORT we injected via env (apps that honour it).
    Deliberately NOT process-tree lsof — on a dev box that picks up unrelated local services
    (Docker, Neo4j, IDEs) that also answer HTTP."""
    ordered: list[int] = []
    seen: set[int] = set()

    def add(p: int):
        if p not in seen:
            seen.add(p)
            ordered.append(p)

    try:
        log = open(log_path, errors="ignore").read()
        for m in re.finditer(r"https?://[^\s/]*?:(\d{2,5})\b", log):
            add(int(m.group(1)))
        for m in re.finditer(r"(?:port|listening on)\D{0,8}(\d{3,5})\b", log, re.I):
            add(int(m.group(1)))
    except Exception:
        pass
    add(port_hint)
    return ordered


#: Lines that carry the REASON a build died, as opposed to the frames underneath it.
_ERROR_LINE_MARKERS = (
    "error", "err!", "failed", "cannot find", "could not resolve", "not found",
    "unexpected", "syntaxerror", "typeerror", "referenceerror", "eaddrinuse",
    "permission denied", "enoent", "missing",
)


def _why_it_failed(log_path: str, limit: int = 900) -> str:
    """The part of a build log that says what went wrong.

    The old version kept the LAST 300 characters, which for a Node stack trace is the least
    informative end of the file — the operator got three frames of
    `rollup/dist/es/shared/parseAst.js` and no error message. Real example, repeated 49 times,
    with nothing anywhere naming the cause.

    So: surface the error LINES first, then the tail for context. A diagnostic that reliably
    omits the diagnosis is worse than no diagnostic, because it looks like one.
    """
    try:
        text = Path(log_path).read_text(errors="ignore")
    except Exception:  # noqa: BLE001
        return "log unavailable."
    return why_it_failed_text(text, limit)


def why_it_failed_text(text: str, limit: int = 900) -> str:
    """_why_it_failed over text in hand — the APP's own section of a start's log
    (app_start.split_output), so an install step's lines are never read as the app's error."""
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "log was empty."
    culprits = [ln for ln in lines
                if any(m in ln.lower() for m in _ERROR_LINE_MARKERS)
                and not ln.lstrip().startswith("at ")]  # stack frames are not the reason
    parts = []
    if culprits:
        # The LAST few matching lines: a build often warns early and dies late.
        parts.append("error: " + " | ".join(culprits[-3:])[:limit])
    parts.append("log tail: " + " ".join(lines[-6:])[:limit])
    return " ".join(parts)


def _discover_working_port(log_path: str, port_hint: int, timeout: float = 20.0,
                           proc: Optional[subprocess.Popen] = None,
                           health_path: str = "/", listener_uid: Optional[int] = None,
                           absent_grace: float = ABSENT_ROUTE_GRACE_SECONDS) -> Optional[int]:
    """Return the port the app actually serves HTTP on — its log-announced port first, then the
    injected PORT — each health-checked, so a neighboring local service is never mistaken for it.

    Given the process, stop waiting the moment it exits: an app that died on an ImportError at
    import time will never bind, and waiting out the full window only delays the real error.

    STOP WAITING THE MOMENT THE ANSWER IS "NO SUCH ROUTE", too. A port that keeps answering
    404/410 at the health path for ABSENT_ROUTE_GRACE_SECONDS has bound and is routing; it is
    not slow, it has nothing there. That was polled every 0.8 s for the whole window — 600 s
    for a build — before it was reported, while the caller held its job."""
    def candidates() -> list[int]:
        ports = _candidate_ports(log_path, port_hint)
        if listener_uid is None:
            return ports
        # A SANDBOXED preview's port is one ITS uid listens on. The log is the app's to write:
        # "listening on 8000" must not make BOSS's own API (or another preview) its preview.
        mine = ports_listened_by(listener_uid)
        return ports if mine is None else [p for p in ports if p in mine]

    deadline = time.time() + timeout
    absent_since: dict[int, float] = {}
    while time.time() < deadline:
        for port in candidates():
            if _http_ok(port, health_path):
                return port
            if _route_absent(port, health_path) is None:
                absent_since.pop(port, None)
            elif time.time() - absent_since.setdefault(port, time.time()) >= absent_grace:
                return None
        if proc is not None and proc.poll() is not None:
            # One last look: a server that forked and exited its parent may be up by now.
            for port in candidates():
                if _http_ok(port, health_path):
                    return port
            return None
        time.sleep(0.8)
    return None


# Names the coding agent's allow-list passes ON PURPOSE because the AGENT needs them to buy
# model turns — and that an app it wrote has no business holding (see app_environment).
_MODEL_CREDENTIAL_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "OPENAI_")


def _is_model_or_credential(name: str) -> bool:
    from .agent_env import SECRET_SHAPED

    upper = name.upper()
    return upper.startswith(_MODEL_CREDENTIAL_PREFIXES) or any(m in upper for m in SECRET_SHAPED)


def app_environment(workdir: Path, port: int, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The environment an app BOSS starts (preview or serve proof) runs with.

    Agent-written code, so the agent's rules: agent_environment withholds DATABASE_URL, deploy
    tokens and every credential-shaped name. This was `{**os.environ, ...}`, so the serve proof
    ran a build's app — code an agent wrote minutes earlier — on the worker with the worker's
    whole environment (second review of f16b5d4), the same exposure that let an agent's test
    suite clear a production database on 2026-09-02 (see agent_env).

    AND THEN LESS THAN THE AGENT. agent_environment is the CODING AGENT's allow-list, and it
    lets ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_BASE_URL through on purpose:
    the agent cannot work without its model credential. b63717c handed that list to the app as
    is, so every serve proof and every host preview started agent-written code holding the
    install's model credential — an app that logs its environment, or a dependency that ships
    it somewhere, spends the operator's model account (third review of b63717c). The app never
    needs it, so every ANTHROPIC_*/CLAUDE_*/OPENAI_* name and every credential-shaped name an
    operator let through BOSS_AGENT_ENV_ALLOW for the agent is removed here.

    PORT and HOST are set AFTER the filter: they are values BOSS chose for this launch, not
    anything inherited, and agent_environment's allow-list (built for the coding agent) does
    not name them. HOME and the npm cache point at the (writable) workdir: the container's
    /home/boss/.npm is owned by root (from the global claude-code install), so `npm install`
    would fail with EACCES ("chown -R 100:101 /home/boss/.npm").
    """
    from .agent_env import agent_environment

    env = agent_environment(os.environ, {
        "PYTHONUNBUFFERED": "1", "npm_config_cache": str(workdir / ".npm-cache"),
        "npm_config_fund": "false", "npm_config_audit": "false",
        "npm_config_update_notifier": "false"})
    env = {k: v for k, v in env.items() if not _is_model_or_credential(k)}
    env.update({"PORT": str(port), "HOST": "0.0.0.0", "HOME": str(workdir)})
    # Cross-app links (F5): values BOSS computed for THIS launch (preview_link_environment),
    # added after the filter like PORT, never inherited.
    env.update(extra or {})
    # ...and never over the launch's own PORT/HOST: launch_app waits on the port it returns, so
    # an `extra` PORT (a boss.deploy.yaml literal `PORT: "3000"`) had the app bind one port
    # while BOSS probed another (the release gate's known-good app, 2026-11-07).
    env.update({"PORT": str(port), "HOST": "0.0.0.0"})
    return env


def launch_app(workdir: Path, command: list[str], log_path: str, *,
               owner: Optional["owned_process.Owner"] = None,
               expires_at=None, port: Optional[int] = None,
               extra_env: Optional[dict[str, str]] = None) -> tuple[subprocess.Popen, int]:
    """Start ONE app the way a preview does: PORT/HOST injected, output to log_path, low
    priority, its own process group. Returns (process, the PORT it was given).

    The single launcher. The build's serve proof (prove_app_serves) and the host preview both
    call it, so "it served when the build checked" and "it serves in preview" are the same
    claim about the same invocation, not two launchers that can drift apart.
    """
    port_hint = port or _free_port()
    env = app_environment(workdir, port_hint, extra_env)
    # THE APP'S OWN OUTPUT, TOLD APART (app_start.marked): an install chained in front of the
    # start prints a marker first, so a failure quotes the app, never npm install's last line.
    from . import app_start as _app_start

    command = _app_start.marked(list(command))
    logf = os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                             0o600), "w")
    try:
        # OWNED (owned_process): the serve proof belongs to the build that runs it (the
        # enclosing owner scope) and dies with it; a preview passes its own `owner`, so it
        # outlives the build that started it and ends when the preview does.
        proc = owned_process.popen(_low_priority(command), cwd=str(workdir), env=env, stdout=logf,
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   owner=owner, expires_at=expires_at,
                                   resource="process_group")
    finally:
        logf.close()
    return proc, port_hint


def _serve_failure(log_path: str, port_hint: int, health_path: str = "/") -> str:
    """Why an app did not serve, in the words the preview has always used. Call BEFORE killing
    it: once it is dead, "never bound" and "bound and served the wrong thing" look the same."""
    listening = _diagnose_listening_port(log_path, port_hint, health_path)
    if listening:
        return listening + " " + _why_it_failed(log_path)
    return "app did not serve HTTP within timeout. " + _why_it_failed(log_path)


# What the implement stage is told to produce. Kept here, beside the reader, so the prompt and
# find_run_declaration cannot describe two different files.
RUN_DECLARATION_INSTRUCTION = (
    "- HOW TO RUN IT (required if what you built serves HTTP — a web app, API or service): "
    "write a `boss.preview.command` file containing ONE shell command that starts the app "
    "and serves HTTP on the port in the `PORT` environment variable (bind 0.0.0.0). Put it in "
    "the app's own directory (the repository root for a root-level app); the command runs from "
    "that directory. For a Python module inside a package use `python3 -m pkg.module`, never "
    "`python3 pkg/module.py` — relative imports fail when a package module runs as a script. "
    "BOSS starts this exact command before the build can pass and fails the build, with the "
    "app's own error, if nothing answers HTTP. A library or CLI with no server writes no such "
    "file; its tests are how it is run.\n"
)


def _proof_tree(workdir: Path) -> Path:
    """A disposable copy of the build's tree to start the app in.

    In place would be wrong: the sandbox's diff is `git add -A`, so whatever the running app
    writes — a SQLite file, an npm cache, a log — would ride into the pull request. There is no
    micro-VM overlay: a micro-VM build is not proved on the host at all (see
    governed_code_task_service.app_serves_check_skip_reason).

    COPIED, NODE_MODULES INCLUDED. b63717c linked each node_modules into the build workdir to
    save the copy, so a declared `npm install && node server.js` — or any app that writes a cache
    under node_modules — wrote THROUGH the link into the build's own tree, and the proof changed
    the workdir it exists to leave alone (third review of b63717c). Nothing the proof's command
    may write is shared with the build: the whole tree is copied, links inside it kept as links
    (npm's .bin entries and workspace links are relative, so they resolve inside the copy).
    """
    # INSIDE THE RUN'S OWN DIRECTORY when the build is isolated. The app is launched as the run's
    # uid, and a mkdtemp under /tmp is BOSS-owned 0700: the run could not even cd into it, so
    # every declared command failed "can't cd to helpdesk-ai" and the app read as not serving
    # (crun_fe2dbbc8, crun_44005c92). The run directory is <uid>:boss 2770, so the copy is
    # reachable by the run and by BOSS and by no other uid.
    run_dir = None
    try:
        from . import sandbox_identity

        ident = sandbox_identity.current()
        if ident is not None and Path(workdir).resolve().is_relative_to(Path(ident.run_dir).resolve()):
            run_dir = Path(ident.run_dir)
    except Exception:  # noqa: BLE001 — not isolated: the old place
        run_dir = None
    proof = Path(tempfile.mkdtemp(prefix="boss-serve-proof-", dir=str(run_dir) if run_dir else None))
    if run_dir is not None:
        os.chmod(proof, 0o2777)
    from .resource_ledger import claim_dir
    claim_dir(proof)
    # workspace_perms.copy_tree, never a bare copytree: the copy is BOSS's and the app runs as
    # the run's uid, so an executable must stay executable for "other" (crun_247435e1:
    # `sh: 1: tsx: Permission denied`, node_modules/tsx/dist/cli.mjs copied 0776).
    from . import workspace_perms

    workspace_perms.copy_tree(workdir, proof,
                              ignore=shutil.ignore_patterns(".git", "__pycache__", ".npm-cache"))
    return proof


# Suffixes of files that can change what an app DOES when started. A build that touched none of
# them (docs, tests, fixtures) changed nothing the serve proof could catch.
_RUNNABLE_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".vue",
                      ".svelte")
_RUNNABLE_NAMES = frozenset({"package.json", RUN_DECLARATION_FILE, DECLARED_APP_FILE,
                             "Procfile", "requirements.txt", "pyproject.toml"})


def touches_runnable_source(changed: Optional[list[str]]) -> bool:
    """Did this build change anything that could alter how its app starts or what it serves?

    A web follow-up that edited only a README or only tests in boss-sandbox — a repo holding
    several apps — has nothing of its own to start, and asking the shared repo which app it is
    gets an honest "ambiguous". Failing that build for not serving was failing it for the
    repository's shape, not for its change.
    """
    for raw in changed or []:
        rel = str(raw).strip().lstrip("./")
        lowered = "/" + rel.lower()
        name = lowered.rsplit("/", 1)[-1]
        if (_is_py_test_path(rel) or "/docs/" in lowered or "/__tests__/" in lowered
                or ".test." in name or ".spec." in name):
            continue
        if name in {n.lower() for n in _RUNNABLE_NAMES} or name.endswith(_RUNNABLE_SUFFIXES):
            return True
    return False


def _entry_is_the_lineages(entry: tuple[str, str], files: list[str]) -> bool:
    """Did this lineage write the app `entry` names? A boss.preview.app naming an app the
    lineage never wrote (a root file merged in from elsewhere) is another app's (how_app_runs).
    """
    kind, value = entry
    value = value.strip().strip("/")
    if kind == "py":
        return value in files
    if value in ("", "."):
        return bool(files)
    return any(f == value or f.startswith(value + "/") for f in files)


def _repo_rel(path) -> str:
    rel = str(path).strip()
    return rel[2:] if rel.startswith("./") else rel


# How each source of a run command is named to a person reading a failure.
_COMMAND_SOURCES = {
    "declared": "the run command the build declared (boss.preview.command)",
    "derived": "the command for the app the build wrote (boss.preview.app)",
    "resolved": "the command the preview resolver chose",
}


def app_serves_failure_sentence(record: Optional[dict]) -> str:
    """ONE sentence for a failed serve proof, used everywhere it is shown.

    The blocked-run error, the convergence finding and the repair prompt each built their own
    from the record, and each said "BOSS ran `(no run command found)`" when nothing was run,
    ended in a dangling "— " when the record had no error, and never said WHICH command it was
    (declared by the build, or guessed). The record already distinguishes "exited (code N)
    before serving" from "started but did not answer HTTP"; this names what ran and where the
    command came from, and never claims a run that did not happen.
    """
    rec = record or {}
    command = rec.get("command")
    error = str(rec.get("error") or "").strip()
    if command:
        what = _COMMAND_SOURCES.get(str(rec.get("source") or ""), "a run command")
        head = f"BOSS ran {what}, `{command}`"
    else:
        head = "BOSS found no command that runs this app"
    return f"{head}: {error}" if error else f"{head}."


def app_serves_headline(record: Optional[dict]) -> str:
    """The failed serve proof as the ONE line a blocked build leads with: "the app did not start:
    tsx: Permission denied" — the gate that failed and the app's first error line, never the
    command or the log tail (those are in app_serves_failure_sentence and the Checks).

    crun_247435e1 ended "Blocked: the template's tests did not pass" although the template's
    tests passed: the connector smoke check could not run because the app never started, and
    the error named the consequence instead of the gate (app_serves) that failed."""
    rec = record or {}
    error = str(rec.get("error") or "").strip()
    if ((rec.get("needs_secret") and error.startswith("the app needs a secret"))
            or (rec.get("optional_unset") and error.startswith("the app must start without"))):
        # THE VARIABLE, by name (templates.run_env): a secret the app needs, or an optional one
        # it crashed without — never only the crash.
        return error.split(". ", 1)[0][:300]
    verb = ("the app did not start" if rec.get("exit_code") is not None or "exited" in error
            else "the app did not serve HTTP")
    line = ""
    if "error: " in error:
        # _why_it_failed's "error: <line> | <line> log tail: …": the first culprit line.
        line = error.split("error: ", 1)[1].split(" log tail:", 1)[0].split(" | ", 1)[0]
    elif error:
        line = error.split(" log tail:", 1)[0]
        if ": " in line and line.startswith("the app "):
            line = line.split(": ", 1)[1]
    line = re.sub(r"^(?:/bin/)?sh: \d+: ", "", line.strip()).strip(" .")
    if not line:
        return verb if rec.get("command") else "BOSS found no command that runs this app"
    return f"{verb}: {line[:300]}"


def probe_route(port: int, path: str, *, timeout: float = 5.0) -> Optional[int]:
    """The HTTP status a GET of `path` on the started app returns (redirects not followed), or
    None when it did not answer."""
    import http.client

    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
        try:
            conn.request("GET", path)
            return int(conn.getresponse().status)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — no answer is the finding
        return None


def _build_services_env(owner) -> dict[str, str]:
    """The run's (or preview's) own build-services variables (DATABASE_URL, PG*), which BOSS
    supplies to the app it starts and which therefore win over "listed/unset" (run_env.supply).
    owned_process injects the same values into a workload's processes; naming them here keeps
    the record honest and a crash that names them from being blamed on a missing setting."""
    try:
        from . import build_services

        return dict(build_services.env_for_owner(owner) or {})
    except Exception:  # noqa: BLE001 — no lease is no services
        return {}


def app_start_environment(app_dir: Optional[Path], *, needs: Optional[dict] = None,
                          bound: Optional[dict[str, str]] = None, purpose: str = "proof"):
    """THE ONE RESOLVER for an app BOSS starts to check it (templates.run_env): what its templates
    declare (`needs`), what its own boss.deploy.yaml and .env.example declare (names only — a
    value in .env.example is never passed), the values a person bound, and the run's build
    services (DATABASE_URL, PG*), which win over "listed/unset". The serve proof and a
    multi-service backend (stack_verify: the verify stage and the template gate) both call it;
    the backend was started with HOME alone, so HelpDesk AI's SESSION_SECRET and DEMO_PASSWORD
    were never minted there (crun_f246d330)."""
    from ..templates.run_env import merge, needs_from_app_dir, resolve

    return resolve(merge(needs or {}, needs_from_app_dir(app_dir)), purpose=purpose, bound=bound,
                   supplied=_build_services_env(owned_process.current_owner()))


def start_record(*, path: str, command, cwd, run_env, log_path: str, exit_code: Optional[int],
                 ok: bool, error: Optional[str] = None, extra: Optional[dict] = None,
                 label: Optional[str] = None) -> dict:
    """app_start.record for a launch_app start: the variables by name and source, the app's own
    output (redacted, every value BOSS gave the launch included)."""
    from . import app_start

    services = _build_services_env(owned_process.current_owner())
    # Every value the app was GIVEN from its declarations and BOSS's services; `extra` holds
    # BOSS's own URLs for this launch (BACKEND_URL), which the output may name.
    secrets = [*(getattr(run_env, "values", {}) or {}).values(), *services.values()]
    return app_start.record(
        path=path, label=label, command=command, cwd=cwd,
        env=app_start.env_sources(run_env=run_env, build_services=services, extra=extra),
        log_text=app_start.read_log(log_path), exit_code=exit_code, ok=ok, error=error,
        secrets=secrets)


def prove_app_serves(workdir: Path, changed: Optional[list[str]], *,
                     lineage: Optional[list[str]] = None, known_service: bool = False,
                     timeout: float = 180.0, probe_paths: Optional[list[str]] = None,
                     needs: Optional[dict] = None, bound: Optional[dict[str, str]] = None) -> dict:
    """START what the build built and require it to answer HTTP — before the build may pass.

    WITH THE ENVIRONMENT THE APP DECLARES (templates.run_env). `needs` is what its templates
    declare (run_env.app_needs), `bound` the values a person bound for its `build` environment;
    the app directory's boss.deploy.yaml and .env.example are read here. A secret BOSS generates
    (a session key) gets a fresh throwaway value for this proof only — HelpDesk AI
    (crun_db538dff) was blocked for "SESSION_SECRET must be set", a value BOSS makes for every
    deploy but never gave the proof. A required secret only a person can set, and nobody has, is
    a "needs a secret" verdict naming it, and the app is not started to crash on it.

    WHY. The Lead Triage Inbox build (crun_3e370c114b95425d8a7c, PR paramg/boss-sandbox#218)
    passed every gate and completed. Its preview then failed twice with "app did not serve HTTP
    within timeout … ImportError: attempted relative import with no known parent package". The
    app was never once started between "tests pass" and "done", so the first run of it was the
    operator's — and the error was reported as a preview timeout, about a build already called
    finished. Tests that import modules prove the modules; only starting the app proves the app.

    `changed` is THIS run's diff; `lineage` is every file the app's earlier runs wrote
    (lineage_impl_files). WHAT IS STARTED is decided by how_app_runs over both — the function
    the preview and the container preview also call — in a copy of the tree with
    the boss.preview.app this build is about to record laid in, so the proof sees exactly what
    the preview will. Given only the diff, a follow-up of a declared app never saw the
    declaration and proved a different command than the preview runs (second review of
    f16b5d4).

    WHO IS HELD TO SERVING is decided by DECLARATION, never by reading code: the lineage (or
    this run) wrote a run command — boss.preview.command, or boss.preview.command in a
    package.json — or `known_service` (the run builds a named full-stack app, official_app_id).
    Every other build — CLI, library, worker, an app that declared nothing — is left to its
    tests, and the record says "not a declared service".

    WHY NOT INFER IT. Three reviews running, the gate guessed "is this a service" from the
    source, and each guess blocked a build that should have completed (the gate is never
    shipped past): cf8c2d4 launched a CLI main.py; f16b5d4 a CLI whose server sat behind a
    subcommand; b63717c (python_source_serves "reachability") still a CLI whose main() picks
    `serve` with an if/elif or a --serve flag — run bare it does its CLI thing and exits, and
    was failed for not answering HTTP. Every smarter heuristic has a next shape. The implement
    prompt REQUIRES a service to declare its run command (RUN_DECLARATION_INSTRUCTION), so the
    build itself says what it is, and the static reading survives only as the preview's guess at
    what to launch for an undeclared repo — it never blocks a build. `web_app_build` is not
    evidence either: it is set on every build-on-existing/extend, CLIs and libraries included.
    A build whose diff changes nothing that can alter how an app starts (docs, tests) has
    nothing to prove.

    A DECLARED command is held to serving whatever it prints — the build said it serves.

    Started with launch_app and waited on with _discover_working_port — the preview's own
    primitives. Never raises. If the CHECK itself cannot run (the tree cannot be copied, the
    launcher fails), the record says `harness: true`: a fact about BOSS, not about the code.
    """
    record: dict = {"ok": True, "proven": False, "source": "none", "command": None,
                    "port": None, "error": None, "app_entry": None, "verification": None,
                    "harness": False}
    this_build = [_repo_rel(c) for c in (changed or []) if str(c).strip()]
    files = this_build + [str(f) for f in (lineage or []) if str(f) not in this_build]
    tree = None
    try:
        if changed is not None and not touches_runnable_source(this_build):
            # Docs/tests only: nothing this build wrote can change what starts.
            record.update(source="no_runnable_change")
            return record
        tree = _proof_tree(workdir)
        # The boss.preview.app this run will push (governed _declare_preview_app computes it
        # the same way), so the derived step sees what the preview will see.
        pending = declare_app_entry(this_build, lambda rel: _read_py(tree, rel))
        if pending:
            (tree / DECLARED_APP_FILE).write_text(pending + "\n", encoding="utf-8")

        def _not_a_service(why: str) -> dict:
            record.update(source="not_declared", not_a_service=f"not a declared service: {why}",
                          verification=_detect_verification(tree))
            return record

        # DECLARED OR NAMED, NOTHING ELSE (see WHO IS HELD above). Asked before how_app_runs so
        # an undeclared build is never launched — and never failed for what the repository
        # holds — on the strength of a guess. how_app_runs asks find_run_declaration the same
        # question with the same files first, so a declaration here is the command run below.
        if not known_service and find_run_declaration(tree, files or None) is None:
            return _not_a_service(
                "the build wrote no boss.preview.command, so its tests are its proof")

        try:
            plan = how_app_runs(tree, files or None)
        except AmbiguousAppError as exc:
            record.update(ok=False, source="ambiguous", error=(
                f"this build serves HTTP but did not declare how to run it, and the "
                f"repository is ambiguous: {exc} Write boss.preview.command."))
            return record
        if plan is None or not plan.command:
            record.update(ok=False, error=(
                "this build serves HTTP but declares no way to run it and none could be "
                "found. Write boss.preview.command with the command that starts it."))
            return record
        if plan.entry:
            record["app_entry"] = f"{plan.entry[0]}:{plan.entry[1]}"

        record["source"] = plan.source
        record["command"] = plan.shell()
        app_dir = _app_dir_of_plan(tree, plan)
        run_env = app_start_environment(app_dir, needs=needs, bound=bound, purpose="proof")
        record["environment"] = run_env.evidence()
        if run_env.needs:
            record.update(ok=False, needs_secret=[n.name for n in run_env.needs], error=(
                f"the app {run_env.verdict()} (environment build), then retry the build. "
                "BOSS did not start it."))
            return record
        log_path = str(tree / ".boss-serve-proof.log")
        proc, port_hint = launch_app(tree, plan.command, log_path, extra_env=run_env.values)
        from . import app_start as _app_start

        def _app_text() -> str:
            return _app_start.split_output(_read_log(log_path))[1]

        try:
            health = preview_health_path(tree)
            port = _discover_working_port(log_path, port_hint,
                                          timeout=preview_start_timeout(_app_dir_of_plan(tree, plan), timeout,
                                                                        command=plan.command),
                                          proc=proc,
                                          health_path=health)
            exit_code = proc.poll()
            if port is not None:
                record.update(proven=True, port=port)
                if probe_paths:
                    # A CONNECTOR'S WIRING SMOKE CHECK (templates.gate.connector_smoke): its
                    # routes answer on the app that just started.
                    record["route_probes"] = {p: probe_route(port, p) for p in probe_paths[:8]}
            elif exit_code is not None:
                # It EXITED; it did not time out. "did not serve HTTP within timeout" for a
                # process that died at 0.8s sent the reader looking for a slow start.
                record.update(ok=False, exit_code=exit_code, error=(
                    f"the app exited (code {exit_code}) before serving HTTP: "
                    + why_it_failed_text(_app_text())))
                log_text = _app_text()
                named = run_env.unset_named_in(log_text)
                if named:
                    # A person's act ONLY for a secret only a person can supply; a crash on a
                    # listed/optional variable is the code's (WORK: the coder's repair round).
                    if run_env.crash_needs_person(log_text):
                        record["needs_secret"] = [named]
                    else:
                        record["optional_unset"] = [named]
                    record["error"] = f"the app {run_env.explain_crash(log_text)}. {record['error']}"
            else:
                listening = _diagnose_listening_port(log_path, port_hint, health)
                record.update(ok=False, error=(
                    f"the app started and kept running but did not answer HTTP within "
                    f"{timeout:g}s. " + (listening + " " if listening else "")
                    + why_it_failed_text(_app_text())))
        finally:
            _kill(proc.pid)
            # THE START'S RECORD, on every outcome (app_start): command, cwd, the variables by
            # name and source, the exit code and the app's own output — on the serve record, in
            # app_logs and as a code_task.app_start event.
            try:
                if record.get("error"):
                    # The error quotes the app's log, and an app may print what it was given: no
                    # value BOSS handed it (a generated key, the run's database password) is kept
                    # on the record, the app_serve_checked event or the repair prompt.
                    record["error"] = _app_start.redactor(
                        [*run_env.values.values(),
                         *_build_services_env(owned_process.current_owner()).values()]
                    )(record["error"])
                record["start"] = start_record(
                    path="serve_proof", label="serve proof", command=record["command"],
                    cwd=app_dir.relative_to(tree).as_posix() if app_dir != tree else ".",
                    run_env=run_env, log_path=log_path, exit_code=record.get("exit_code"),
                    ok=bool(record.get("proven")), error=record.get("error"))
                _app_start.publish(record["start"])
            except Exception:  # noqa: BLE001 — bookkeeping never fails the proof
                logger.debug("serve proof: start record not kept", exc_info=True)
        return record
    except Exception as exc:  # noqa: BLE001 — the check reports, it never crashes the build
        record.update(ok=False, harness=True,
                      error=f"could not start the app to check it: "
                            f"{type(exc).__name__}: {exc}"[:600])
        return record
    finally:
        if tree is not None:
            shutil.rmtree(tree, ignore_errors=True)
            from .resource_ledger import release_dir
            release_dir(tree, "serve_proof_done")


class PreviewNotReady(Exception):
    """A preview was requested for a run that has not produced a reviewable change yet.

    Deliberately NOT a RuntimeError: the API tries several preview substrates in turn and
    treats RuntimeError as "this substrate failed, try the next one". Retrying three ways
    cannot conjure a PR, so this must propagate on the first attempt instead of being
    swallowed into a generic "preview could not start".
    """

    def __init__(self, run_id: str, status: str, phase: str) -> None:
        self.run_id, self.status, self.phase = run_id, status, phase
        super().__init__(
            f"run {run_id} has not opened a pull request yet "
            f"(status={status}, phase={phase}), so there is no completed change to preview"
        )

    def as_detail(self) -> dict:
        return {
            "error": "preview_unavailable_no_pr",
            "message": ("This run has not opened a pull request yet, so there is no completed "
                        "change to preview."),
            "run_id": self.run_id, "run_status": self.status, "run_phase": self.phase,
            "next_action": ("Wait for the run to finish. If it is blocked on a plan-approval "
                            "decision, approve it; if it failed, retry the task."),
        }


def _verify_green(run: dict) -> bool:
    state = run.get("state") or {}
    if state.get("tests_passed") is True:
        return True
    cmd = str(state.get("test_command") or "").lower()
    return state.get("test_exit_code") == 0 and "verify" in cmd


def find_local_run_workspace(run_id: str) -> Optional[Path]:
    """Retained local-subprocess sandbox for a verify-green run (no PR required)."""
    roots = [
        Path(tempfile.gettempdir()) / "boss-sandboxes",
        Path("/app"),
        Path("/var/lib/boss"),
        Path("/tmp"),
        PREVIEW_ROOT.parent,
    ]
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = str(root.resolve())
        except OSError:
            continue
        if resolved in seen or not root.is_dir():
            continue
        seen.add(resolved)
        for cand in root.glob(f"{run_id}*"):
            if not cand.is_dir():
                continue
            if workspace_app_root(cand) is not None:
                return cand
    return None


def workspace_app_root(workspace: Path) -> Optional[Path]:
    """Where the app is inside a retained workspace (app_root's rule): the workspace itself when
    it carries an app manifest (package.json, pyproject.toml, pom.xml, …), else its one app
    folder. Not "a top-level package.json": a Python or Java app, or an app in a subfolder, is
    previewable too (BOSSAPP-920)."""
    from . import app_root as _app_root

    if _app_root._has_manifest(workspace):  # noqa: SLF001 — the module's own manifest rule
        return workspace
    apps = _app_root.subfolder_apps(workspace)
    if len(apps) == 1:
        return workspace / apps[0]
    return None


def github_wait_mirror(run: dict, settings=None, *, any_status: bool = False) -> Optional[Path]:
    """BOSSAPP-922: the local mirror that holds a GitHub build's branch while GitHub is down.

    A build that ended `waiting_for_github` committed its work into the repository's bare mirror
    (<repo root>/_mirrors/<owner>/<repo>.git); the push and the pull request come later. Until
    then GitHub has no branch to clone, so its preview clones the mirror instead of failing.
    `any_status` also answers for a run in any other state (a GitHub clone that just failed on an
    outage). None when the run's repository is local, has no mirror, or the mirror lacks the
    branch."""
    from . import local_repo
    from .github_outage import WAITING_STATUS

    if not any_status and str(run.get("status") or "") != WAITING_STATUS:
        return None
    spec = run.get("spec") or {}
    owner = str(spec.get("owner") or getattr(settings, "target_github_owner", "") or "")
    repo = str(spec.get("repo") or getattr(settings, "target_github_repo", "") or "")
    if not owner or not repo or local_repo.is_local_owner(owner):
        return None
    fctx = run.get("finalize_ctx") or {}
    branch = fctx.get("work_branch") or f"boss/code/{fctx.get('root_run_id') or run.get('id')}"
    try:
        mirror = local_repo.mirror_path(owner, repo, settings)
        if (mirror / "HEAD").is_file() and local_repo.branch_exists(mirror, branch):
            return mirror
    except local_repo.LocalRepoError:
        return None
    return None


def assert_previewable(run: dict, run_id: str) -> None:
    """A preview is meaningful once the work exists as a PR *or* verify-green files.

    Gated on the PR for GitHub-backed runs. A local-subprocess run that already
    passed ``npm run verify`` is also previewable from its retained workspace —
    review-timeout / no-PR must not hide working software.
    """
    if run.get("pull_request_url"):
        return
    if _verify_green(run) and find_local_run_workspace(run_id) is not None:
        return
    # Built and committed while GitHub was down: the work exists, in the local mirror.
    if github_wait_mirror({**run, "id": run.get("id") or run_id}) is not None:
        return
    raise PreviewNotReady(run_id, str(run.get("status") or "?"), str(run.get("phase") or "?"))


def _record_repo_manifests(engine, run_id: str, workdir: Path) -> None:
    """Record what the app's repository declares about how it runs, when the build did not.

    Builds made before the build recorded `repo_manifests` are read here, from the same branch
    head the preview runs, so a retried preview and the deploy after it see the app's own
    boss.deploy.yaml (app_deploy_manifest.app_manifest). Never fails a preview."""
    try:
        from .app_deploy_manifest import manifests_in

        found = manifests_in(workdir)
        with begin(engine) as c:
            c.execute(
                text("UPDATE code_task_runs SET state = COALESCE(state, '{}'::jsonb) "
                     "|| jsonb_build_object('repo_manifests', CAST(:m AS jsonb)) "
                     "WHERE id = :r AND NOT (COALESCE(state, '{}'::jsonb) ? 'repo_manifests')"),
                {"r": run_id, "m": json.dumps(found)})
    except Exception:  # noqa: BLE001
        logger.warning("preview could not record the deploy manifests of %s", run_id,
                       exc_info=True)


def start_preview(engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60,
                  ready_timeout: Optional[float] = None) -> dict:
    """Clone the run's branch, launch the app, discover its port, register an expiring preview.

    `ready_timeout` caps how long the app may take to answer (the auto-preview's bound)."""
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    assert_previewable(run, run_id)
    spec = run.get("spec") or {}
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    # Iterations reuse the ROOT run's branch (boss/code/{root_run_id}); a run's own
    # id may have no branch. Prefer an explicit work_branch, else the root-derived one.
    _fctx = run.get("finalize_ctx") or {}
    branch = _fctx.get("work_branch") or f"boss/code/{_fctx.get('root_run_id') or run_id}"
    build_substrate = _fctx.get("execution_substrate") or normalize_build_substrate(
        settings.sandbox_provider
    ).value
    project_id = run["project_id"]
    preview_id = make_id("prev")

    log_path = preview_log_path(preview_id)
    workdir = Path(tempfile.gettempdir())     # set below, once the sandbox is held

    def _fail(msg: str, *, command: Optional[str] = None, exited: Optional[bool] = None) -> dict:
        # THE LAUNCH COMMAND IS PART OF THE RECORD. app_previews.command was empty on every
        # failed row, so "it crashed" could not be followed by "running WHAT" — the one fact
        # needed to reproduce it by hand.
        failure_kind, suggested_action = _classify_start_failure(msg, exited=exited)
        _release_preview_sandbox(preview_id)
        with begin(engine) as c:
            c.execute(text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,command,substrate,"
                "build_substrate,status,error,failure_kind,log_path,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,:cmd,:sub,:build,'failed',:e,:fk,:lg,now())"),
                {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}",
                 "b": branch, "cmd": command, "sub": ExecutionSubstrate.PREVIEW_HOST.value,
                 "build": build_substrate, "e": msg[:500], "fk": failure_kind,
                 "lg": log_path})
            # A FAILURE NOBODY IS TOLD ABOUT.
            #
            # `app_preview.started` was emitted on success and nothing at all on failure, so a
            # preview that died wrote its reason into a row and stopped there. The Activity Log
            # is a projection of events — it cannot show what was never emitted — so an
            # operator sent to the log to find out why their preview failed found agent turns
            # and no preview at all, and reasonably concluded the log was useless.
            emit_event(
                c, project_id, "app_preview.failed", "operator", "app_preview", preview_id,
                {"run_id": run_id, "repo": f"{owner}/{repo}", "branch": branch,
                 "failure_kind": failure_kind, "suggested_action": suggested_action,
                 "error": msg[:500], "log_path": log_path},
            )
        return {"id": preview_id, "status": "failed", "error": msg, "command": command,
                "failure_kind": failure_kind, "suggested_action": suggested_action}

    from .sandbox_identity import SandboxUnavailable

    try:
        ident = _preview_sandbox(preview_id)
    except SandboxUnavailable as exc:
        return _fail(f"the preview sandbox is unavailable: {exc}")
    workdir = _preview_workdir(preview_id, ident)
    local_ws = None if (run.get("pull_request_url") or "") else find_local_run_workspace(run_id)
    if local_ws is not None:
        src = workspace_app_root(local_ws) or local_ws
        try:
            from . import workspace_perms

            # Modes carried over and, sandboxed, opened to the preview's uid executables included.
            workspace_perms.copy_tree(src, workdir, ignore=shutil.ignore_patterns("node_modules"),
                                      symlinks=False, open_to_run=ident is not None)
        except OSError as exc:
            return _fail(f"copy local workspace failed: {exc}")
    else:
        # NOT settings.github_token. That field is the old install-wide way of naming a
        # target repo and is empty wherever the repository was connected through the
        # wizard, so this handed git an anonymous URL for a private repo and every clone
        # died on "could not read Username for 'https://github.com'". See
        # github_connections.github_token_for.
        # The previewed app's code runs in this checkout: no token may be in it (IDENTITY.md
        # §7.10) — through egress under a delegated principal, else in git's environment only.
        from boss_engine.identity import runs as _identity_runs
        from . import local_repo as _local_repo
        from .transient_retry import is_transient_git_error

        def _clone_mirror(mirror: Path):
            # BOSSAPP-922: the branch is committed in the repository's local mirror and GitHub
            # does not have it yet. file://, no credential, no egress.
            return owned_process.boss_run(
                ["git", "clone", "--depth", "1", "--branch", branch,
                 _local_repo.file_url(mirror), str(workdir)],
                capture_output=True, text=True, timeout=120, env=_local_repo._env(),  # noqa: SLF001
                **({"umask": 0} if ident is not None else {}))

        def _token() -> str:
            with begin(engine) as _c:
                return github_token_for(_c, settings, project_id, owner, repo) or ""

        _run_ref = {**run, "id": run.get("id") or run_id}
        waiting_mirror = github_wait_mirror(_run_ref, settings)
        if waiting_mirror is not None:
            clone = _clone_mirror(waiting_mirror)
        else:
            with _identity_runs.repo_access(engine, host=getattr(settings, "target_repo_host", "github") or "github",
                                            owner=owner, repo=repo, project_id=project_id, token=_token) as _access:
                clone_url = _access.url
                clone = owned_process.boss_run(["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(workdir)],
                                   capture_output=True, text=True, timeout=120, env=_access.boss_environ(),
                                   # BOSS's git, writing a tree the preview's uid must be able to edit
                                   **({"umask": 0} if ident is not None else {}))
            if clone.returncode != 0 and is_transient_git_error(clone.stderr):
                # GitHub did not answer: the mirror has the same branch when the build left it there.
                fallback = github_wait_mirror(_run_ref, settings, any_status=True)
                if fallback is not None:
                    for leftover in list(workdir.iterdir()) if workdir.is_dir() else []:
                        (shutil.rmtree(leftover, ignore_errors=True) if leftover.is_dir()
                         and not leftover.is_symlink() else leftover.unlink(missing_ok=True))
                    clone = _clone_mirror(fallback)
        if clone.returncode != 0:
            return _fail(f"git clone failed: {(clone.stderr or '')[-300:]}")
    _record_repo_manifests(engine, run_id, workdir)

    def _verify(cmd_str: str) -> dict:
        """Library/CLI/module build — there is no web server to serve. 'Run it' means run its
        tests: execute the suite one-shot, capture output, and return the result so the user sees
        it work, instead of a misleading 'add a preview command' error."""
        try:
            # The app's own tests: its code, so the preview's sandbox uid and the app's
            # environment, never the control plane's.
            env = app_environment(workdir, 0)
            env.pop("PORT", None)
            proc = owned_process.run(["sh", "-lc", cmd_str], cwd=str(workdir), env=env,
                                  owner=owned_process.Owner("preview", preview_id, project_id),
                                  capture_output=True, text=True, timeout=300)
            output = (proc.stdout or "") + (proc.stderr or "")
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            output, exit_code = "test run exceeded 300s and was stopped", 124
        finally:
            _release_preview_sandbox(preview_id)
        try:
            Path(log_path).write_text(output, encoding="utf-8")
        except OSError:
            pass
        passed = exit_code == 0
        status = "verified" if passed else "failed"
        with begin(engine) as c:
            c.execute(text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,command,mode,substrate,"
                "build_substrate,status,failure_kind,error,log_path,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,:cmd,'test',:sub,:build,:st,:fk,:err,:lg,now())"),
                {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}",
                 "b": branch, "cmd": cmd_str, "sub": ExecutionSubstrate.PREVIEW_HOST.value,
                 "build": build_substrate, "st": status,
                 "fk": None if passed else "tests_failed",
                 "err": None if passed else "tests failed — see output", "lg": log_path})
        return {
            "id": preview_id, "status": status, "mode": "test", "kind": "library",
            "command": cmd_str, "exit_code": exit_code, "passed": passed,
            "output": output[-6000:],
            "message": (
                f"Verified by tests — NOT a live app preview. No runnable entrypoint was detected "
                f"(looked for: boss.preview.command, package.json dev/start, Procfile web, Makefile "
                f"dev/run, {'/'.join(_PY_ENTRYPOINTS)}, index.html in {'/'.join(_STATIC_DIRS[1:])}), "
                f"so this is either a library/module or an app whose entrypoint is named something "
                f"unrecognised. Ran its test suite (`{cmd_str}`): "
                f"{'PASSED ✓' if passed else 'FAILED ✗'} (exit {exit_code}). See the output below "
                f"and the PR to review the code."),
        }

    # `impl_files` is what THIS task built — prefer its entrypoint over any
    # pre-existing app already in the repo.
    _changed = lineage_impl_files(engine, run)
    command = _detect_command(workdir, _changed)
    if not command:
        # No web server, but a library still has a way to 'run it' — its tests. Prefer the test
        # command the pipeline already recorded on the run, else detect one from the repo.
        test_cmd = (run.get("state") or {}).get("test_command") or _detect_verification(workdir)
        if test_cmd:
            return _verify(test_cmd)
        return _fail(
            "no runnable app or test suite found in this build (add boss.preview.command, a "
            "package.json dev/start/test script, a Procfile web command, a Makefile dev/run/test "
            "target, or a Python entrypoint/tests)"
        )

    database_refused = provision_preview_database(engine, run, preview_id, workdir)
    if database_refused:
        return _fail(database_refused, command=" ".join(command))
    _expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    # WHAT THE APP DECLARES, resolved by the rule the build's proof and the deploy use
    # (templates.run_env): a session key BOSS generates is fresh for this preview; a secret only a
    # person can set, and nobody has, is named here instead of crashing the app.
    app_env = preview_app_environment(engine, project_id=project_id, run_id=run_id,
                                      preview_id=preview_id, app_dir=app_dir_of(workdir, _changed))
    if app_env.needs:
        return _fail(f"the app {app_env.verdict()} (environment preview), then start the preview again",
                     command=" ".join(command))
    proc, port_hint = launch_app(
        workdir, command, log_path,
        owner=owned_process.Owner("preview", preview_id, project_id),
        expires_at=_expires, port=_sandboxed_preview_port(ident),
        extra_env=preview_link_environment(engine, project_id=project_id, run_id=run_id,
                                           preview_id=preview_id, expires_at=_expires, rotate=True,
                                           app_env=app_env))

    # Web builds do `npm install` + `vite build` before the static server binds — well past the
    # default 20s window — so give discovery generous headroom (it returns as soon as the port
    # answers, so this never slows the fast case).
    # Given the process, discovery stops the moment it exits — an app that died on an
    # ImportError never binds, and waiting out three minutes only delayed the real error.
    health = preview_health_path(workdir)
    window = preview_start_timeout(workdir, changed=_changed, limit=ready_timeout, command=command)
    port = _discover_working_port(log_path, port_hint, timeout=window, proc=proc,
                                  health_path=health,
                                  listener_uid=ident.uid if ident is not None else None)
    if port is None:
        # NAME WHICH FAILURE THIS IS before killing the process — once it is dead, the
        # difference between "never bound" and "bound and served the wrong thing" is gone.
        exit_code = proc.poll()
        from . import app_start as _app_start

        _own = _app_start.split_output(_read_log(log_path))[1]
        why = (
            f"the app exited (code {exit_code}) before serving HTTP: " + why_it_failed_text(_own)
            if exit_code is not None else _serve_failure(log_path, port_hint, health)
        )
        needed = app_env.explain_crash(_own)
        if needed:
            why = f"the app {needed}. {why}"
        # No value the preview was given survives into its failure (an app may print its env).
        why = _app_start.redactor(list(app_env.values.values()))(why)
        _kill(proc.pid)
        try:
            _app_start.publish(start_record(
                path="preview", label=f"preview {preview_id}", command=command, cwd=".",
                run_env=app_env, log_path=log_path, exit_code=exit_code, ok=False, error=why),
                engine=engine, project_id=project_id, run_id=run_id)
        except Exception:  # noqa: BLE001 — bookkeeping never hides the preview's own failure
            logger.debug("preview %s: start record not kept", preview_id, exc_info=True)
        return _fail(why, command=" ".join(command), exited=exit_code is not None)

    # ITS OWN HTTPS ORIGIN (BOSSAPP-860), only with BOSS_PREVIEW_EXPOSE on: mapped by the
    # boss-expose helper and verified from outside before the preview is marked running.
    from . import preview_expose

    try:
        _exposed = preview_expose.attach(preview_id, port)
    except preview_expose.ExposeFailed as exc:
        _kill(proc.pid)
        return _fail(f"expose: {exc}", command=" ".join(command), exited=False)

    url = f"http://127.0.0.1:{port}"
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO app_previews (id,project_id,run_id,repo,branch,command,port,url,pid,"
            "substrate,build_substrate,status,log_path,expires_at) "
            "VALUES (:id,:p,:r,:repo,:b,:cmd,:port,:url,:pid,:sub,:build,'running',:lg,:exp)"),
            {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}", "b": branch,
             "cmd": " ".join(command), "port": port, "url": url, "pid": proc.pid,
             "sub": ExecutionSubstrate.PREVIEW_HOST.value, "build": build_substrate,
             "lg": log_path, "exp": expires})
        emit_event(c, project_id, "app_preview.started", "operator", "app_preview", preview_id,
                   {"run_id": run_id, "url": url, "repo": f"{owner}/{repo}", "branch": branch})
    demo_logins = record_demo_logins(engine, preview_id, app_env)
    return _with_exposed(engine, preview_id, _exposed, publicize_preview({
        "id": preview_id, "status": "running", "url": url, "port": port, "demo_logins": demo_logins,
        "pid": proc.pid, "expires_at": expires.isoformat(), "run_id": run_id,
        "repo": f"{owner}/{repo}", "branch": branch,
        "substrate": ExecutionSubstrate.PREVIEW_HOST.value,
        "build_substrate": build_substrate,
    }, settings=settings))


def _with_exposed(engine, preview_id: str, exposed: Optional[dict], out: dict) -> dict:
    """BOSSAPP-860: record and return the preview's verified own origin. Unchanged when None."""
    if not exposed:
        return out
    with begin(engine) as c:
        c.execute(text("UPDATE app_previews SET exposed_url = :u WHERE id = :id"),
                  {"u": exposed["url"], "id": preview_id})
    return {**out, "exposed_url": exposed["url"], "open_url": exposed["url"]}


def _java_build_of(app_dir: Path) -> Optional[str]:
    """"maven" | "gradle" | None for a directory the app resolver already chose (how_app_runs:
    the declaring directory, or resolve_app_entry's java entry) — never a repository root asked
    on its own (app_root's rule)."""
    from . import toolchains

    if (Path(app_dir) / "package.json").is_file():
        return None                      # a Node app beside a pom.xml is still a Node app
    return toolchains.detect_java(app_dir)


def _generate_dockerfile(workdir: Path, changed: Optional[list[str]] = None) -> int:
    """Ensure the app has a Dockerfile; return the container port the app listens on.
    Generated images set HOST=0.0.0.0 + PORT so deploy-ready apps bind correctly for -p mapping."""
    existing = workdir / "Dockerfile"
    if existing.exists():
        m = re.search(r"EXPOSE\s+(\d{2,5})", existing.read_text(errors="ignore"))
        return int(m.group(1)) if m else 8000
    # Prefer the entrypoint THIS task built. Sharing _PY_ENTRYPOINTS between the host and
    # container paths was not enough: the preference logic lived only in _detect_command, so a
    # docker-mode preview still picked the repo's pre-existing serve.py and launched Sudoku for
    # an N-Queens build (PR #62). Both paths must make the SAME choice, not merely consult the
    # same list.
    #
    # And the same ANSWER as the host preview and the serve proof: how_app_runs. (Not the
    # deploy, which keeps its production commands.)
    # This path read resolve_app_entry alone, so a build's declared run command — the one the
    # build proved serves — was ignored here exactly as the deploy ignored it.
    plan = how_app_runs(workdir, changed)
    if plan is not None and plan.source == "declared":
        rel = plan.declared_in or "."
        app_dir = "/app" if rel == "." else f"/app/{rel}"
        command = json.dumps(plan.declared_command)
        java = _java_build_of(workdir / rel)
        if (workdir / rel / "package.json").is_file():
            port = 3000
            setup = f"RUN {NPM_INSTALL_CMD}\n"
            base = "node:20-slim"
        elif java:
            # A JAVA APP'S OWN RUN COMMAND (`mvn spring-boot:run`, `java -jar …`): it needs
            # the JDK and its build tool, which a python image does not have — this branch used
            # to give every non-Node declaration python:3.11-slim.
            from . import toolchains

            port = toolchains.JAVA_PORT
            setup = ""
            base = toolchains.JAVA_BUILD_IMAGES[java]
        else:
            port = 8000
            setup = ("RUN pip install --no-cache-dir -r requirements.txt\n"
                     if (workdir / rel / "requirements.txt").is_file() else "")
            base = "python:3.11-slim"
        existing.write_text(
            f"FROM {base}\nCOPY . /app\nWORKDIR {app_dir}\n" + setup +
            f"ENV HOST=0.0.0.0 PORT={port} PYTHONUNBUFFERED=1\nEXPOSE {port}\n"
            f'CMD ["sh", "-lc", {command}]\n')
        return port
    kind, value = plan.entry if plan is not None and plan.entry else ("", "")
    if kind == "static":
        # Serve the static app THIS task built. Without this branch the container path fell
        # through to the repo-wide python entrypoint and launched the wrong application.
        port = 8000
        existing.write_text(
            "FROM python:3.11-slim\nWORKDIR /app\nCOPY . .\n"
            f"ENV HOST=0.0.0.0 PORT={port}\nEXPOSE {port}\n"
            f'CMD ["python3", "-m", "http.server", "{port}", "--bind", "0.0.0.0", '
            f'"--directory", "{value}"]\n')
        return port
    py_entry = value if kind == "py" else None
    if py_entry:
        port = 8000
        reqs = "COPY requirements.txt ./\nRUN pip install --no-cache-dir -r requirements.txt\n" \
            if (workdir / "requirements.txt").exists() else ""
        # Same invocation as the host path: a package module runs with -m from above its
        # package, or its relative imports fail exactly as they did on the host.
        run_dir, argv = python_entry_invocation(workdir, py_entry)
        cmd = ", ".join(json.dumps(a) for a in argv)
        app_dir = "/app" if run_dir == "." else f"/app/{run_dir}"
        existing.write_text(
            "FROM python:3.11-slim\nWORKDIR /app\n" + reqs +
            f"COPY . .\nWORKDIR {app_dir}\n"
            f"ENV HOST=0.0.0.0 PORT={port} PYTHONUNBUFFERED=1\nEXPOSE {port}\n"
            f'CMD [{cmd}]\n')
        return port
    if kind == "java":
        # The resolver chose a Java app (pom.xml / build.gradle): build its jar with the build
        # tool's image, run it on a JRE. It used to fall through to "could not infer a stack".
        from . import toolchains

        app_root = workdir if value in (".", "") else workdir / value
        build = _java_build_of(app_root)
        if build:
            existing.write_text(toolchains.java_dockerfile(
                build, value or ".", config=toolchains.java_config_file(app_root)))
            return toolchains.JAVA_PORT
    if kind == "node":
        port = 3000
        app_workdir = "/app" if value in (".", "") else f"/app/{value}"
        existing.write_text(
            "FROM node:20-slim\n"
            "COPY . /app\n"
            f"WORKDIR {app_workdir}\n"
            f"RUN {NPM_INSTALL_CMD}\n"
            "RUN npm run build --if-present || true\n"
            f"ENV HOST=0.0.0.0 PORT={port}\nEXPOSE {port}\n"
            "CMD [\"npm\", \"run\", \"start\"]\n")
        return port
    if (workdir / "package.json").exists():
        port = 3000
        existing.write_text(
            "FROM node:20-slim\nWORKDIR /app\nCOPY package*.json ./\n"
            f"RUN {NPM_INSTALL_CMD}\nCOPY . .\n"
            "RUN npm run build --if-present || true\n"
            f"ENV HOST=0.0.0.0 PORT={port}\nEXPOSE {port}\nCMD [\"npm\", \"run\", \"start\"]\n")
        return port
    raise RuntimeError("no Dockerfile and could not infer a stack (server.py/app.py/package.json)")


def start_container_preview(engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60) -> dict:
    """Tier-2 dev-container preview: build a Docker image from the run's branch and run it with a
    mapped port — isolated, reproducible, the same image that later ships to remote compute."""
    if shutil.which("docker") is None:
        raise RuntimeError("docker is not available on this host")
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    assert_previewable(run, run_id)
    spec = run.get("spec") or {}
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    # Iterations reuse the ROOT run's branch (boss/code/{root_run_id}); a run's own
    # id may have no branch. Prefer an explicit work_branch, else the root-derived one.
    _fctx = run.get("finalize_ctx") or {}
    branch = _fctx.get("work_branch") or f"boss/code/{_fctx.get('root_run_id') or run_id}"
    build_substrate = _fctx.get("execution_substrate") or normalize_build_substrate(
        settings.sandbox_provider
    ).value
    project_id = run["project_id"]
    preview_id = make_id("prev")
    tag = preview_image_tag(preview_id)
    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"{preview_id}-", dir=str(PREVIEW_ROOT)))

    def _fail(msg: str, *, command: Optional[str] = None) -> dict:
        failure_kind, suggested_action = _classify_start_failure(msg)
        with begin(engine) as c:
            c.execute(text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,command,mode,substrate,"
                "build_substrate,status,error,failure_kind,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,:cmd,'docker',:sub,:build,'failed',:e,:fk,now())"),
                {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}",
                 "b": branch, "cmd": command, "sub": ExecutionSubstrate.PREVIEW_DOCKER.value,
                 "build": build_substrate, "e": msg[:500], "fk": failure_kind})
            # Same reason as the host path: a failure that emits nothing cannot appear in a log
            # that is a projection of events.
            emit_event(
                c, project_id, "app_preview.failed", "operator", "app_preview", preview_id,
                {"run_id": run_id, "repo": f"{owner}/{repo}", "branch": branch, "mode": "docker",
                 "failure_kind": failure_kind, "suggested_action": suggested_action,
                 "error": msg[:500]},
            )
        return {"id": preview_id, "status": "failed", "error": msg, "mode": "docker",
                "failure_kind": failure_kind, "suggested_action": suggested_action}

    # NOT settings.github_token — see github_connections.github_token_for. Empty wherever the
    # repository was connected through the wizard, which made every clone of a private repo
    # fail on "could not read Username for 'https://github.com'".
    # The previewed app's code runs in this checkout: no token may be in it (IDENTITY.md
    # §7.10) — through egress under a delegated principal, else in git's environment only.
    from boss_engine.identity import runs as _identity_runs

    def _token() -> str:
        with begin(engine) as _c:
            return github_token_for(_c, settings, project_id, owner, repo) or ""

    with _identity_runs.repo_access(engine, host=getattr(settings, "target_repo_host", "github") or "github",
                                    owner=owner, repo=repo, project_id=project_id, token=_token) as _access:
        clone_url = _access.url
        clone = owned_process.boss_run(["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(workdir)],
                           capture_output=True, text=True, timeout=120, env=_access.boss_environ())
    if clone.returncode != 0:
        return _fail(f"git clone failed: {(clone.stderr or '')[-300:]}")

    try:
        _changed_ep = lineage_impl_files(engine, run)
        container_port = _generate_dockerfile(workdir, _changed_ep)
    except RuntimeError as exc:
        return _fail(str(exc))

    # LABELLED, so the reaper's substrate sweep finds this image and its container by what
    # they are (boss.owner / boss.preview_id / boss.install_id) even when the row is gone.
    labels = preview_labels(engine, preview_id)
    build = owned_process.boss_run(["docker", "build", *labels, "-t", tag, str(workdir)],
                              capture_output=True, text=True, timeout=600)
    if build.returncode != 0:
        _docker_rmi(tag)
        return _fail(f"docker build failed: {(build.stderr or build.stdout or '')[-400:]}")

    # THE SAME ENVIRONMENT the host preview and the build's proof give the app (templates.run_env),
    # through a mode-600 env file — never `-e NAME=value` in argv, where `ps` would show it.
    app_env = preview_app_environment(engine, project_id=project_id, run_id=run_id,
                                      preview_id=preview_id, app_dir=app_dir_of(workdir, _changed_ep))
    if app_env.needs:
        _docker_rmi(tag)
        return _fail(f"the app {app_env.verdict()} (environment preview), then start the preview again")
    env_file_args: list[str] = []
    env_file_path: Optional[str] = None
    if app_env.values:
        fd, env_file_path = tempfile.mkstemp(prefix="boss-preview-env-")
        with os.fdopen(fd, "w") as fh:
            fh.write("".join(f"{k}={v}\n" for k, v in sorted(app_env.values.items())
                             if "\n" not in v and "\r" not in v))
        env_file_args = ["--env-file", env_file_path]
    host_port = _free_port()
    docker_argv = ["docker", "run", "-d", "--init", *container_limits("preview_node"), *labels,
                   "--name", tag, "-p", f"{host_port}:{container_port}",
                   "-e", f"PORT={container_port}", "-e", "HOST=0.0.0.0", *env_file_args, tag]
    docker_command = " ".join(docker_argv)
    try:
        run_cmd = owned_process.boss_run(docker_argv, capture_output=True, text=True, timeout=60)
    finally:
        if env_file_path:
            with contextlib.suppress(OSError):
                os.unlink(env_file_path)
    if run_cmd.returncode != 0:
        _docker_rm(tag)
        _docker_rmi(tag)
        return _fail(f"docker run failed: {(run_cmd.stderr or '')[-300:]}", command=docker_command)
    container_id = run_cmd.stdout.strip()[:12]

    url = f"http://127.0.0.1:{host_port}"
    ok = False
    for _ in range(20):
        if _http_ok(host_port):
            ok = True
            break
        time.sleep(1.0)
    if not ok:
        logs = owned_process.boss_run(["docker", "logs", "--tail", "20", tag], capture_output=True,
                                 text=True, timeout=30).stdout
        _docker_rm(tag)
        _docker_rmi(tag)
        return _fail(f"container did not serve HTTP. logs: {logs[-300:]}", command=docker_command)

    from . import preview_expose

    try:
        _exposed = preview_expose.attach(preview_id, host_port)
    except preview_expose.ExposeFailed as exc:
        _docker_rm(tag)
        _docker_rmi(tag)
        return _fail(f"expose: {exc}", command=docker_command)

    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO app_previews (id,project_id,run_id,repo,branch,command,mode,container_id,"
            "port,url,substrate,build_substrate,status,expires_at) "
            "VALUES (:id,:p,:r,:repo,:b,:cmd,'docker',:cid,:port,:url,:sub,:build,'running',:exp)"),
            {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}", "b": branch,
             "cmd": docker_command, "cid": container_id, "port": host_port, "url": url,
             "sub": ExecutionSubstrate.PREVIEW_DOCKER.value, "build": build_substrate,
             "exp": expires})
        emit_event(c, project_id, "app_preview.started", "operator", "app_preview", preview_id,
                   {"run_id": run_id, "url": url, "mode": "docker", "image": tag})
    demo_logins = record_demo_logins(engine, preview_id, app_env)
    return _with_exposed(engine, preview_id, _exposed, publicize_preview({
        "id": preview_id, "status": "running", "url": url, "port": host_port, "mode": "docker",
        "demo_logins": demo_logins,
        "container_id": container_id, "image": tag, "expires_at": expires.isoformat(),
        "run_id": run_id, "repo": f"{owner}/{repo}", "branch": branch,
        "substrate": ExecutionSubstrate.PREVIEW_DOCKER.value,
        "build_substrate": build_substrate,
    }, settings=settings))


def start_rootfs_preview(
    engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60,
    command: str = "",
) -> dict:
    """Serve a micro-VM run from its retained build rootfs (no host-side re-clone)."""
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    assert_previewable(run, run_id)
    fctx = run.get("finalize_ctx") or {}
    state = run.get("state") or {}
    build_substrate = fctx.get("execution_substrate") or state.get("execution_substrate")
    if build_substrate != ExecutionSubstrate.MICROVM.value:
        raise RuntimeError("rootfs preview is only available for a micro-VM build")
    sandbox_ids = fctx.get("sandbox_task_ids") or state.get("sandbox_task_ids") or []
    sandbox_id = fctx.get("sandbox_task_id") or (sandbox_ids[-1] if sandbox_ids else None)
    if not sandbox_id:
        raise RuntimeError("micro-VM build has no retained sandbox_task_id")

    provider = MicroVMProvider(settings)
    provider.hold(
        sandbox_id,
        ttl_seconds=max(60, int(ttl_minutes) * 60),
        reason=f"rootfs preview for {run_id}",
    )
    served = provider.serve_preview(
        sandbox_id,
        ttl_seconds=max(60, int(ttl_minutes) * 60),
        command=command,
    )
    remote_url = served.get("url") or served.get("proxy_url")
    if not remote_url:
        raise RuntimeError("sandbox service did not return a rootfs preview URL")
    preview_id = make_id("prev")
    spec = run.get("spec") or {}
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    branch = fctx.get("work_branch") or f"boss/code/{fctx.get('root_run_id') or run_id}"
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    with begin(engine) as c:
        c.execute(
            text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,command,url,mode,"
                "substrate,build_substrate,sandbox_task_id,status,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,:cmd,:url,'rootfs',:sub,:build,:sid,'running',:exp)"
            ),
            {"id": preview_id, "p": run["project_id"], "r": run_id,
             "repo": f"{owner}/{repo}", "b": branch, "cmd": served.get("command") or command,
             "url": remote_url, "sub": ExecutionSubstrate.PREVIEW_ROOTFS.value,
             "build": build_substrate, "sid": sandbox_id, "exp": expires},
        )
        emit_event(
            c, run["project_id"], "app_preview.started", "operator", "app_preview", preview_id,
            {"run_id": run_id, "mode": "rootfs", "sandbox_task_id": sandbox_id,
             "substrate": ExecutionSubstrate.PREVIEW_ROOTFS.value},
        )
    return publicize_preview({
        "id": preview_id, "status": "running", "mode": "rootfs", "url": remote_url,
        "substrate": ExecutionSubstrate.PREVIEW_ROOTFS.value,
        "build_substrate": build_substrate, "sandbox_task_id": sandbox_id,
        "expires_at": expires.isoformat(), "run_id": run_id,
        "repo": f"{owner}/{repo}", "branch": branch,
    }, settings=settings)


def _process_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _docker_alive(container_id: Optional[str]) -> bool:
    if not container_id or shutil.which("docker") is None:
        return False
    result = owned_process.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
        capture_output=True, text=True, timeout=5, sandboxed=False,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _respawn_process_preview(engine, settings: Settings, preview_id: str) -> None:
    """Re-launch a dead process-mode preview IN PLACE — same preview id, so its
    /v1/previews/<id>/web URL survives an api restart/redeploy. Re-clones the branch, rebuilds,
    serves, then flips the SAME row back to 'running' with the new pid/port. Runs in a background
    thread (the clone+build takes ~60s) so it never blocks api boot."""
    try:
        with begin(engine) as c:
            row = c.execute(text(
                "SELECT run_id,project_id,repo,branch,build_substrate,expires_at FROM app_previews WHERE id=:id"
            ), {"id": preview_id}).mappings().first()
        if row is None:
            return
        project_id = row["project_id"]
        repo_full = row["repo"] or ""
        owner, _, repo = repo_full.partition("/")
        branch = row["branch"]
        if not owner or not repo or not branch:
            return
        ident = _preview_sandbox(preview_id)       # SandboxUnavailable: stays stopped (below)
        workdir = _preview_workdir(preview_id, ident)
        log_path = preview_log_path(preview_id)
        # NOT settings.github_token. That field is the old install-wide way of naming a
        # target repo and is empty wherever the repository was connected through the
        # wizard, so this handed git an anonymous URL for a private repo and every clone
        # died on "could not read Username for 'https://github.com'". See
        # github_connections.github_token_for.
        # The previewed app's code runs in this checkout: no token may be in it (IDENTITY.md
        # §7.10) — through egress under a delegated principal, else in git's environment only.
        from boss_engine.identity import runs as _identity_runs

        def _token() -> str:
            with begin(engine) as _c:
                return github_token_for(_c, settings, project_id, owner, repo) or ""

        with _identity_runs.repo_access(engine, host=getattr(settings, "target_repo_host", "github") or "github",
                                        owner=owner, repo=repo, project_id=project_id, token=_token) as _access:
            clone_url = _access.url
            clone = owned_process.run(
            ["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(workdir)],
            capture_output=True, text=True, timeout=120, env=_access.boss_environ(), sandboxed=False,
            **({"umask": 0} if ident is not None else {}))
        if clone.returncode != 0:
            _release_preview_sandbox(preview_id)
            return
        _changed = lineage_impl_files(engine, {"id": row["run_id"]})
        command = _detect_command(workdir, _changed)
        if not command:
            _release_preview_sandbox(preview_id)
            return
        with begin(engine) as c:
            _run = load_code_task_run(c, row["run_id"]) if row.get("run_id") else None
        if provision_preview_database(engine, _run, preview_id, workdir):
            _release_preview_sandbox(preview_id)
            return
        # The one launcher, so a respawned preview gets the same filtered environment as the
        # first start (app_environment) instead of the worker's own.
        app_env = preview_app_environment(engine, project_id=project_id, run_id=row.get("run_id"),
                                          preview_id=preview_id, app_dir=app_dir_of(workdir, _changed))
        if app_env.needs:
            _release_preview_sandbox(preview_id)
            return
        record_demo_logins(engine, preview_id, app_env)
        proc, port_hint = launch_app(
            workdir, command, log_path,
            owner=owned_process.Owner("preview", preview_id, row.get("project_id")),
            port=_sandboxed_preview_port(ident),
            extra_env=preview_link_environment(engine, project_id=project_id, run_id=row.get("run_id"),
                                               preview_id=preview_id, expires_at=row.get("expires_at"),
                                               rotate=False, app_env=app_env))
        port = _discover_working_port(log_path, port_hint,
                                      timeout=preview_start_timeout(workdir, changed=_changed,
                                                                    command=command),
                                      health_path=preview_health_path(workdir),
                                      listener_uid=ident.uid if ident is not None else None)
        if port is None:
            _kill(proc.pid)
            _release_preview_sandbox(preview_id)
            return
        url = f"http://127.0.0.1:{port}"
        with begin(engine) as c:
            c.execute(text(
                "UPDATE app_previews SET status='running', pid=:pid, port=:port, url=:url, "
                "log_path=:lg WHERE id=:id"),
                {"pid": proc.pid, "port": port, "url": url, "lg": log_path, "id": preview_id})
    except Exception:  # noqa: BLE001 — best-effort; a failed respawn just leaves it 'stopped'
        _release_preview_sandbox(preview_id)
        return


# How many dead process previews to auto-respawn on boot (newest first) — bounds the rebuild
# cost so a restart with many historical previews doesn't stampede npm builds.
# ONE, and only one worth its rebuild (stability spec §4a.6). Each boot rebuilt up to three
# previews — clone, install, build — while the build the restart interrupted was redoing the
# same, which is a load spike at the worst moment. A preview about to expire is not worth it,
# and none is respawned while the host has no headroom (admission.respawn_allowed).
_RESPAWN_ON_BOOT_LIMIT = 1
_RESPAWN_MIN_REMAINING = timedelta(minutes=10)


def reconcile_previews(engine, settings: Settings) -> dict[str, int]:
    """Idempotently reconcile durable preview rows against their live substrate."""
    with begin(engine) as c:
        rows = c.execute(text(
            "SELECT id,pid,mode,container_id,url,expires_at,sandbox_task_id FROM app_previews "
            "WHERE status='running'"
        )).mappings().all()
    now = datetime.now(timezone.utc)
    alive = stopped = expired = 0
    headers = ({"X-Sandbox-Token": settings.sandbox_token}
               if settings.sandbox_token else {})
    respawn_candidates: list[str] = []   # dead process-mode previews to relaunch in place
    for raw in rows:
        row = dict(raw)
        expires_at = row.get("expires_at")
        mode = row.get("mode") or "process"
        if expires_at and expires_at <= now:
            target_status = "expired"
        else:
            substrate_alive = (
                _docker_alive(row.get("container_id")) if mode == "docker"
                else _url_ok(row.get("url") or "", headers=headers) if mode == "rootfs"
                else _process_alive(row.get("pid"))
            )
            healthy = substrate_alive and _url_ok(
                row.get("url") or "", headers=headers if mode == "rootfs" else None
            )
            target_status = "running" if healthy else "stopped"
        if target_status == "running":
            alive += 1
            continue
        if target_status == "expired":
            _teardown(row, settings)
        with begin(engine) as c:
            c.execute(
                text("UPDATE app_previews SET status=:status WHERE id=:id AND status='running'"),
                {"status": target_status, "id": row["id"]},
            )
        if target_status == "expired":
            expired += 1
        else:
            stopped += 1
            # A process preview (web app) that died with the old container — respawn it IN PLACE so
            # its URL survives the restart. rootfs/docker/test modes reconcile on their own.
            if mode == "process":
                respawn_candidates.append(row["id"])

    # Bound the rebuild cost: only relaunch the newest few (they're the ones anyone is looking at).
    respawned = 0
    if respawn_candidates:
        with begin(engine) as c:
            ordered = [r[0] for r in c.execute(text(
                "SELECT id FROM app_previews WHERE id = ANY(:ids) AND expires_at > :keep "
                "ORDER BY created_at DESC LIMIT :lim"
            ), {"ids": respawn_candidates, "lim": _RESPAWN_ON_BOOT_LIMIT,
                "keep": now + _RESPAWN_MIN_REMAINING}).all()]
        if ordered and not _respawn_allowed(engine, settings):
            ordered = []
        for pid in ordered:
            threading.Thread(target=_respawn_process_preview, args=(engine, settings, pid),
                             daemon=True).start()
            respawned += 1
    return {"checked": len(rows), "running": alive, "stopped": stopped, "expired": expired,
            "respawning": respawned}


def _respawn_allowed(engine, settings: Settings) -> bool:
    """Whether the host has room to rebuild a preview now (the admission gate's answer)."""
    try:
        from .admission import respawn_allowed
        return respawn_allowed(engine, settings)
    except ImportError:
        return True
    except Exception:  # noqa: BLE001 — unknown capacity: be conservative at boot
        return False


def reap_previews(engine, settings: Settings, *, idle_minutes: Optional[int] = None,
                  now: Optional[datetime] = None) -> dict[str, int]:
    """THE PERIODIC PREVIEW REAPER (stability spec §4a.6). Expiry used to happen only when
    somebody listed previews or the API booted, so an unlisted 60-minute preview ran forever.

    * past its TTL → torn down, `expired`;
    * its process or container is gone → `stopped`;
    * nobody has opened it (no proxied request) for `idle_minutes` → torn down, `stopped`.
    """
    now = now or datetime.now(timezone.utc)
    if idle_minutes is None:
        idle_minutes = int(os.environ.get("BOSS_PREVIEW_IDLE_MINUTES") or 20)
    with begin(engine) as c:
        rows = [dict(r) for r in c.execute(text(
            "SELECT id, project_id, pid, mode, container_id, sandbox_task_id, expires_at, "
            "created_at, last_seen_at FROM app_previews WHERE status = 'running'"
        )).mappings().all()]
    out = {"checked": len(rows), "expired": 0, "dead": 0, "idle": 0}
    for row in rows:
        mode = row.get("mode") or "process"
        if mode == FIXTURE_MODE:
            continue
        expires = row.get("expires_at")
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        seen = row.get("last_seen_at") or row.get("created_at")
        if seen is not None and getattr(seen, "tzinfo", None) is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if expires is not None and expires <= now:
            status, why, key = "expired", "past its time limit", "expired"
        elif mode == "process" and not _process_alive(row.get("pid")):
            status, why, key = "stopped", "its process exited", "dead"
        elif mode == "docker" and not _docker_alive(row.get("container_id")):
            status, why, key = "stopped", "its container exited", "dead"
        elif idle_minutes and seen is not None and now - seen > timedelta(minutes=idle_minutes):
            status, why, key = "stopped", f"nobody opened it for {idle_minutes} min", "idle"
        else:
            continue
        try:
            _teardown(row, settings)
        except Exception:  # noqa: BLE001 — mark it anyway; the substrate sweep follows up
            pass
        with begin(engine) as c:
            changed = c.execute(text(
                "UPDATE app_previews SET status = :s, error = COALESCE(error, :e) "
                "WHERE id = :id AND status = 'running'"),
                {"s": status, "e": f"Stopped by BOSS: {why}.", "id": row["id"]}).rowcount
            if changed:
                try:
                    with c.begin_nested():
                        emit_event(c, row.get("project_id") or "", f"app_preview.{status}",
                                   "boss", "app_preview", row["id"],
                                   {"reason": key, "detail": why})
                except Exception:  # noqa: BLE001 — the stop stands without its log line
                    pass
        out[key] += 1
    return out


def preview_log_snapshot(engine, preview_id: str, *, tail_chars: int = 12000) -> dict:
    with begin(engine) as c:
        row = c.execute(text(
            "SELECT status,mode,log_path,container_id,failure_kind,error FROM app_previews WHERE id=:id"
        ), {"id": preview_id}).mappings().first()
    if row is None:
        raise ValueError("preview not found")
    content = ""
    if row["log_path"]:
        try:
            content = Path(row["log_path"]).read_text(errors="ignore")[-tail_chars:]
        except OSError:
            content = ""
    elif row["mode"] == "docker" and row["container_id"] and shutil.which("docker"):
        result = owned_process.run(
            ["docker", "logs", "--tail", "200", row["container_id"]],
            capture_output=True, text=True, timeout=10, sandboxed=False,
        )
        content = (result.stdout + result.stderr)[-tail_chars:]
    return {"preview_id": preview_id, "status": row["status"], "mode": row["mode"],
            "failure_kind": row["failure_kind"], "error": row["error"], "log": content}


def stream_preview_logs(engine, preview_id: str):
    """SSE generator that tails startup logs until the preview reaches a terminal state."""
    offset = 0
    for _ in range(600):  # bounded to ten minutes; clients may reconnect
        snapshot = preview_log_snapshot(engine, preview_id)
        content = snapshot["log"]
        if len(content) > offset:
            chunk = content[offset:]
            offset = len(content)
            yield f"event: log\ndata: {json.dumps(chunk)}\n\n"
        else:
            yield ": heartbeat\n\n"
        if snapshot["status"] in {"failed", "stopped", "expired"}:
            yield f"event: end\ndata: {json.dumps(snapshot['status'])}\n\n"
            return
        time.sleep(1)


def _docker_rm(container_id: Optional[str]) -> None:
    if container_id:
        try:
            owned_process.boss_run(["docker", "rm", "-f", container_id], capture_output=True,
                              timeout=60)
        except Exception:  # noqa: BLE001 — the reaper's substrate sweep is the backstop
            pass


def _docker_rmi(tag: Optional[str]) -> None:
    """A preview's image goes with it. Teardown was `rm -f` only: 82 `boss-prev-*` images
    (29 GB nominal) were left on one laptop, one per preview ever started."""
    if tag:
        try:
            owned_process.boss_run(["docker", "rmi", "-f", tag], capture_output=True, timeout=120)
        except Exception:  # noqa: BLE001
            pass


def preview_image_tag(preview_id: str) -> str:
    return f"boss-prev-{preview_id.replace('_', '-').lower()}"


def preview_labels(engine, preview_id: str) -> list[str]:
    """`--label` args every preview container and image carries (stability spec §4a.6)."""
    install = ""
    try:
        from .resource_ledger import install_id
        with begin(engine) as c:
            install = install_id(c)
    except Exception:  # noqa: BLE001 — unlabelled by install still has owner + preview id
        install = ""
    out = ["--label", "boss.owner=preview", "--label", f"boss.preview_id={preview_id}"]
    if install:
        out += ["--label", f"boss.install_id={install}"]
    return out


def container_limits(kind: str) -> list[str]:
    """The resource envelope a preview container runs in (workload_profiles): a memory cap,
    a reservation, CPUs, a pids bound, and the OOM killer's first pick — so one preview can
    never take the machine the control plane runs on."""
    from .workload_profiles import container_args
    return container_args(kind)


def _kill(pid: Optional[int]) -> None:
    """End a preview's process TREE. The one this process started is ended through its owner
    (group + token, verified); a pid read back from a row after a restart falls back to its
    group, never to a bare pid that may since belong to something else."""
    if not pid:
        return
    for proc in owned_process.live():
        if proc.pid == int(pid):
            proc.end(reason="preview_stopped", grace=2.0)
            return
    try:
        owned_process.terminate_tree(int(pid), None, grace=2.0)
    except Exception:  # noqa: BLE001 — best effort; the reaper's orphan sweep is the backstop
        pass


#: What the shim keeps in `window.name` (per tab, kept across a reload) so the preview origin's
#: root-path bootstrap (apps/api/app/preview_origin.py) can send a reload back to its preview.
PREVIEW_TAB_NAME = "boss-preview:"


def _inject_shim(html: bytes, prefix: str, *, at_root: bool = False) -> bytes:
    """Make a path-prefixed preview behave like an app mounted at `/`.

    The server-side rewrite handles initial markup/CSS; the client shim covers
    dynamic DOM, fetch/XHR, history routing, WebSocket/EventSource, and captures
    preview-anchored operator feedback.

    `at_root` (the preview ORIGIN, which serves previews and nothing else): THE APP'S ROUTER SEES
    ITS OWN PATHS. A client-side router (React Router's BrowserRouter, createBrowserRouter, any
    router reading `location.pathname`) matched "/v1/previews/<id>/web/" against its "/" route,
    matched nothing and rendered a blank page (HelpDesk AI, 2026-10-16) — and nothing an app can
    do about a prefix it never heard of. So before the app's scripts run the shim replaces the
    address with the app's own path ("/v1/previews/<id>/web/tickets" → "/tickets"), and history
    calls stay unprefixed (a prefixed one is unprefixed). Requests (fetch, XHR, assets, sockets,
    links and forms) are still sent under the prefix, through the gateway; a RELOAD of "/tickets"
    is sent back to this preview by the origin's bootstrap, from the tab's `window.name`.
    Elsewhere (the API origin) the address keeps its prefix, as before.
    """
    prefix = prefix.rstrip("/")
    p_json = json.dumps(prefix)
    feedback_url = json.dumps(prefix.rsplit("/web", 1)[0] + "/feedback")
    preview_id = prefix.rsplit("/web", 1)[0].rsplit("/", 1)[-1]
    shim = (
        "<base href=\"" + prefix + "/\">"
        "<script data-boss-preview-shim>(function(){var P=" + p_json + ",F=" + feedback_url + ","
        "R=" + ("true" if at_root else "false") + ",I=" + json.dumps(PREVIEW_TAB_NAME + preview_id)
        + ";"
        # AT ROOT: the app's own path in the address bar before any of its scripts reads it.
        "if(R){try{var lp=location.pathname;if(lp===P||lp.indexOf(P+\"/\")===0)"
        "history.replaceState(history.state,\"\",(lp.slice(P.length)||\"/\")+location.search+"
        "location.hash);}catch(e){}"
        "try{var wn=String(window.name||\"\");if(!wn||wn.indexOf(" + json.dumps(PREVIEW_TAB_NAME)
        + ")===0)window.name=I;}catch(e){}}"
        "function path(u){if(typeof URL!==\"undefined\"&&u instanceof URL)u=u.href;"
        "if(typeof u!==\"string\")return u;var o=location.origin;"
        "if(o&&o!==\"null\"&&u.indexOf(o+\"/\")===0&&u.indexOf(o+P)!==0)return o+P+u.slice(o.length);"
        "if(u.charAt(0)===\"/\"&&u.slice(0,2)!==\"//\"&&u.indexOf(P)!==0)return P+u;return u;}"
        "function unpath(u){if(typeof URL!==\"undefined\"&&u instanceof URL)u=u.href;"
        "if(typeof u!==\"string\")return u;var o=location.origin;"
        "if(o&&u.indexOf(o+P)===0)u=u.slice(o.length);if(u===P)return \"/\";"
        "if(u.indexOf(P)===0&&\"/?#\".indexOf(u.charAt(P.length))>=0){u=u.slice(P.length);"
        "return u.charAt(0)===\"/\"?u:\"/\"+u;}return u;}"
        "function srcset(v){return String(v||\"\").split(\",\").map(function(x){var p=x.trim().split(/\\s+/);"
        "p[0]=path(p[0]);return p.join(\" \").trim();}).join(\", \");}"
        "function ws(u){u=path(u);if(typeof u===\"string\"&&u.charAt(0)===\"/\")"
        "return(location.protocol===\"https:\"?\"wss:\":\"ws:\")+\"//\"+location.host+u;return u;}"
        "var of=window.fetch;window.fetch=function(u,o){return of.call(this,path(u),o);};"
        "var xo=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){"
        "arguments[1]=path(u);return xo.apply(this,arguments);};"
        "[\"pushState\",\"replaceState\"].forEach(function(k){var old=history[k];"
        "history[k]=function(s,t,u){return old.call(this,s,t,R?unpath(u):path(u));};});"
        "var OW=window.WebSocket;if(OW){window.WebSocket=function(u,p){"
        "return p===undefined?new OW(ws(u)):new OW(ws(u),p);};window.WebSocket.prototype=OW.prototype;}"
        "var OE=window.EventSource;if(OE){window.EventSource=function(u,o){return new OE(path(u),o);};"
        "window.EventSource.prototype=OE.prototype;}"
        "function fix(root){if(!root||!root.querySelectorAll)return;"
        "root.querySelectorAll(\"[href],[src],[action],[srcset],[style]\").forEach(function(el){"
        "[\"href\",\"src\",\"action\"].forEach(function(a){var v=el.getAttribute(a);"
        "if(v)el.setAttribute(a,path(v));});var ss=el.getAttribute(\"srcset\");if(ss)el.setAttribute(\"srcset\",srcset(ss));"
        "var st=el.getAttribute(\"style\");if(st)el.setAttribute(\"style\",st.replace(/url\\((['\"]?)\\/(?!\\/)/g,\"url($1\"+P+\"/\"));});}"
        "new MutationObserver(function(ms){ms.forEach(function(m){m.addedNodes.forEach(fix);});})"
        ".observe(document.documentElement,{childList:true,subtree:true});"
        "document.addEventListener(\"DOMContentLoaded\",function(){fix(document);"
        "if(document.querySelector(\"[data-boss-feedback]\"))return;var b=document.createElement(\"button\");"
        "b.textContent=\"Send preview feedback\";b.setAttribute(\"data-boss-feedback\",\"1\");"
        "b.style.cssText=\"position:fixed;right:16px;bottom:16px;z-index:2147483647;padding:9px 13px;"
        "border:0;border-radius:999px;background:#111827;color:white;font:600 12px system-ui;cursor:pointer\";"
        "b.onclick=async function(){var message=prompt(\"What should BOSS change here?\");if(!message)return;"
        "var route=location.pathname.indexOf(P)===0?(location.pathname.slice(P.length)||\"/\"):location.pathname;"
        "var a=document.activeElement,sel=a?String(a.tagName||\"\").toLowerCase()+"
        "(a.id?\"#\"+a.id:\"\")+(a.className&&typeof a.className===\"string\"?\".\"+a.className.trim().replace(/\\s+/g,\".\"):\"\"):\"\";"
        "var r=await of.call(window,F,{method:\"POST\",headers:{\"Content-Type\":\"application/json\"},"
        "body:JSON.stringify({message:message,anchor:route,selected_element:sel})});"
        "var d=await r.json();alert(d.question||\"Feedback captured. Confirm the proposed iteration in BOSS.\");};"
        "document.body.appendChild(b);});})();</script>"
    )
    try:
        s = html.decode("utf-8", "ignore")
        # Initial static resources and CSS URLs must be prefixed before the browser
        # starts fetching them. Protocol-relative/external/fragment URLs are untouched.
        def attr_rewrite(match: re.Match) -> str:
            value = match.group("value")
            if value.startswith("/") and not value.startswith("//") and not value.startswith(prefix):
                value = prefix + value
            return f"{match.group('attr')}={match.group('quote')}{value}{match.group('quote')}"

        s = re.sub(
            r"(?P<attr>href|src|action)=(?P<quote>['\"])(?P<value>[^'\"]*)(?P=quote)",
            attr_rewrite, s, flags=re.I,
        )
        def srcset_rewrite(match: re.Match) -> str:
            candidates = []
            for candidate in match.group("value").split(","):
                pieces = candidate.strip().split()
                if pieces and pieces[0].startswith("/") and not pieces[0].startswith("//"):
                    pieces[0] = prefix + pieces[0]
                candidates.append(" ".join(pieces))
            return f"srcset={match.group('quote')}{', '.join(candidates)}{match.group('quote')}"

        s = re.sub(
            r"srcset=(?P<quote>['\"])(?P<value>[^'\"]*)(?P=quote)",
            srcset_rewrite, s, flags=re.I,
        )
        s = re.sub(r"url\((['\"]?)/(?!/)", rf"url(\1{prefix}/", s, flags=re.I)
        if re.search(r"<head(?:\s[^>]*)?>", s, flags=re.I):
            s = re.sub(r"(<head(?:\s[^>]*)?>)", lambda m: m.group(1) + shim, s, count=1, flags=re.I)
        elif "<html" in s.lower():
            s = re.sub(r"(<html[^>]*>)", lambda m: m.group(1) + "<head>" + shim + "</head>", s,
                       count=1, flags=re.I)
        else:
            s = shim + s
        return s.encode("utf-8")
    except Exception:  # noqa: BLE001
        return html


# A PREVIEW IS SOMEONE ELSE'S CODE ON THE CONSOLE'S ORIGIN. The console serves previews at
# /v1/previews/<id>/web/... on its own host, so without isolation a previewed app's script could
# call /v1/identity/me with the operator's session, read the CSRF token and act as them. Every
# preview response therefore carries a CSP sandbox WITHOUT allow-same-origin: the browser gives
# the document an opaque origin, its requests carry no SameSite cookie, and it can read nothing
# the console's origin serves. The preview reaches only its own capability URL, which carries no
# credential (the console's proxy forwards none there), so a wildcard CORS answer is safe and is
# what lets an opaque-origin page read its own backend.
PREVIEW_SANDBOX_CSP = (
    "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
)
# ON THE PREVIEW ORIGIN, THE APP KEEPS ITS OWN COOKIES (BOSSAPP-946). Without allow-same-origin
# the page's origin is opaque: every fetch to its own backend is cross-site, the browser drops
# the session cookie its sign-in sets, and a login app stops at its sign-in page (the
# local-first HelpDesk on localhost:3001). The preview origin (apps/api/app/preview_origin.py)
# is a listener that serves previews and nothing else, never BOSS's own origin
# (preview_origin_for refuses one), strips every BOSS credential on the way in and relays only
# this preview's tagged cookies on the way out — so the page may have a real origin there.
# The console's own origin keeps PREVIEW_SANDBOX_CSP (opaque): a preview answered there is
# never same-origin with BOSS.
PREVIEW_ORIGIN_SANDBOX_CSP = PREVIEW_SANDBOX_CSP + " allow-same-origin"


def preview_isolation_headers() -> dict[str, str]:
    """The headers every preview web/feedback response carries (see PREVIEW_SANDBOX_CSP)."""
    return {
        "Content-Security-Policy": PREVIEW_SANDBOX_CSP,
        "Access-Control-Allow-Origin": "*",
    }


# HEADERS THE PREVIEWED APP MUST NOT SEE. Everything else is forwarded.
#
# This was an ALLOWLIST of four names, and it broke every app that invented a header of its
# own. The first casualty was ApproveIt's X-Acting-User-Id, which was fixed by appending it to
# the list — the second was an app whose client sends X-Request-ID as an idempotency key on
# POST /api/verifications. Its server, correctly, refuses a request without one:
#
#     ❌ X-Request-ID header is required
#
# The app was right and its client did send the header. The PROXY removed it. An operator sees
# an app BOSS built failing in a preview BOSS serves, with an error that blames the app.
#
# An allowlist cannot work here. BOSS does not know what the apps it writes will invent, and
# adding a name per app means every new app is broken until somebody notices. The real
# requirement is narrow and knowable in advance — do not hand the tenant process BOSS's own
# credentials, and do not forward headers that describe THIS hop rather than the request. So
# the rule is inverted: forward everything except those.
#
# Authorization and X-Boss-Api-Key are the control-plane secrets. Cookie is denied for the
# same reason and it matters more than it looks: previews are served from the API's own origin,
# so the browser attaches BOSS's cookies to preview requests, and forwarding them would hand a
# tenant app the operator's session.
_PREVIEW_BLOCKED_HEADER_NAMES = frozenset({
    # BOSS credentials. Never.
    "authorization", "proxy-authorization", "cookie",
    "x-boss-api-key", "x-ops-token", "x-api-key",
    # This hop, not the request. Forwarding them corrupts the upstream request: the app would
    # be told the wrong Host and a Content-Length that no longer matches the body we send.
    "host", "content-length", "connection", "keep-alive", "proxy-connection",
    "transfer-encoding", "te", "trailer", "upgrade", "expect",
})


def forwarded_preview_headers(
    incoming: Optional[dict] = None,
    *,
    content_type: Optional[str] = None,
) -> dict[str, str]:
    """The incoming headers a preview app may receive: everything not explicitly blocked.

    See _PREVIEW_BLOCKED_HEADER_NAMES for why this is a denylist. In short: an allowlist has to
    be extended for every header an app invents, and until it is, the app fails in the preview
    with an error that reads as the app's fault.
    """
    out: dict[str, str] = {}
    for key, value in (incoming or {}).items():
        if value is None:
            continue
        name = str(key).strip()
        if not name or name.lower() in _PREVIEW_BLOCKED_HEADER_NAMES:
            continue
        out[name] = str(value)
    if content_type and "content-type" not in {k.lower() for k in out}:
        out["Content-Type"] = content_type
    return out


#: preview id → when its last_seen_at was last written. Idle-stop needs "seen in the last N
#: minutes", not every request: one UPDATE a minute per preview at most.
_SEEN_WRITTEN: dict[str, float] = {}


def _touch_seen(engine, preview_id: str) -> None:
    now = time.monotonic()
    if now - _SEEN_WRITTEN.get(preview_id, 0.0) < 60:
        return
    _SEEN_WRITTEN[preview_id] = now
    try:
        with begin(engine) as c:
            c.execute(text("UPDATE app_previews SET last_seen_at = now() WHERE id = :id"),
                      {"id": preview_id})
    except Exception:  # noqa: BLE001 — a missed touch only shortens an idle preview's life
        pass


def proxy_preview(engine, preview_id: str, subpath: str, method: str,
                  query_string: str = "", body: Optional[bytes] = None,
                  content_type: Optional[str] = None,
                  sandbox_token: str = "",
                  incoming_headers: Optional[dict] = None,
                  app_cookie_header: Optional[str] = None,
                  serve_at_root: bool = False):
    """Reverse-proxy a request to a running preview's loopback port → (status, content_type, bytes,
    headers). Only the api can reach the preview's 127.0.0.1 port, so this exposes it via the api's
    own (tailnet-routed) URL. HTML responses get the root-relative path-rewrite shim.

    Cookies (preview_cookies): `app_cookie_header` is the app's OWN cookies, already filtered and
    untagged by the caller (the incoming Cookie header is never forwarded); the app's Set-Cookie
    answers come back under headers["Set-Cookie"] as a LIST, each tagged and scoped to this
    preview's path, the ones naming a BOSS cookie or another path dropped.

    `serve_at_root`: the request came through the preview origin, so the app is shown at its own
    paths (see `_inject_shim`)."""
    from .preview_cookies import outbound_set_cookie
    import urllib.error
    with begin(engine) as c:
        row = c.execute(text("SELECT url,status,mode FROM app_previews WHERE id=:id"),
                        {"id": preview_id}).mappings().first()
    if row is None:
        return 404, "text/plain", b"preview not found", {}
    if row["status"] != "running":
        return 409, "text/plain", f"preview not running (status={row['status']})".encode(), {}
    _touch_seen(engine, preview_id)
    if str(row.get("mode") or "") == FIXTURE_MODE:
        if subpath and subpath not in {"", "/"}:
            return 404, "text/plain", b"fixture preview has no extra paths", {}
        return 200, "text/html; charset=utf-8", fixture_html(preview_id), {}
    if not row["url"]:
        return 409, "text/plain", f"preview not running (status={row['status']})".encode(), {}
    target = row["url"].rstrip("/") + "/" + (subpath or "")
    if query_string:
        target += "?" + query_string
    req = urllib.request.Request(target, data=body or None, method=method)
    for header_name, header_value in forwarded_preview_headers(
        incoming_headers, content_type=content_type
    ).items():
        req.add_header(header_name, header_value)
    if app_cookie_header:
        req.add_header("Cookie", app_cookie_header)
    if row["mode"] == "rootfs" and sandbox_token:
        req.add_header("X-Sandbox-Token", sandbox_token)
    response_headers: dict = {}

    def _cookies_of(message) -> None:
        made = [c for c in (outbound_set_cookie(preview_id, v) for v in (message.get_all("Set-Cookie") or []))
                if c]
        if made:
            response_headers["Set-Cookie"] = made

    try:
        resp = urllib.request.build_opener(_NoRedirect()).open(req, timeout=25)
        status = resp.status
        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        data = resp.read()
        if resp.headers.get("Location"):
            response_headers["Location"] = resp.headers["Location"]
        _cookies_of(resp.headers)
    except urllib.error.HTTPError as e:
        status, ctype, data = e.code, e.headers.get("Content-Type", "text/plain"), e.read()
        if e.headers.get("Location"):
            response_headers["Location"] = e.headers["Location"]
        _cookies_of(e.headers)
    except Exception as e:  # noqa: BLE001 — process gone (e.g. after an api restart): self-heal
        try:
            with begin(engine) as c2:
                c2.execute(text("UPDATE app_previews SET status='stopped' WHERE id=:id AND status='running'"),
                           {"id": preview_id})
        except Exception:
            pass
        return 502, "text/html", (
            "<div style=\"font-family:system-ui;padding:24px;max-width:520px\">"
            "<h3>Preview is no longer running</h3>"
            "<p>Its process ended (this happens after a redeploy). Go back to the code task and click "
            "<b>Run preview</b> to regenerate it.</p>"
            f"<p style=\"color:#888;font-size:12px\">{e}</p></div>"
        ).encode(), {}
    location = response_headers.get("Location")
    if location:
        response_headers["Location"] = _gateway_location(preview_id, location)
    if "text/html" in (ctype or ""):
        data = _inject_shim(data, f"/v1/previews/{preview_id}/web", at_root=serve_at_root)
    return status, ctype, data, response_headers


def _is_boss_oidc(parsed: urllib.parse.SplitResult) -> bool:
    """A redirect to BOSS's OWN OIDC provider (Sign in with BOSS) at its public origin. On a local
    install that origin is loopback too (http://localhost:<port>), and the loopback rule below would
    glue the authorize URL under the preview's prefix: every sign-in inside a preview broke."""
    try:
        from ..config import get_settings

        own = urllib.parse.urlsplit(str(get_settings().public_url or "").strip())
    except Exception:  # noqa: BLE001
        return False
    if not own.scheme or not own.netloc:
        return False
    return (parsed.scheme.lower(), parsed.netloc.lower()) == (own.scheme.lower(), own.netloc.lower()) \
        and parsed.path.startswith("/oidc/")


def _gateway_location(preview_id: str, location: str) -> str:
    """A previewed app's redirect, pointed back through the gateway.

    The app is reached at 127.0.0.1:<port>, so a framework that builds ABSOLUTE redirects sends
    `http://127.0.0.1:<port>/login` — which in the operator's browser is their own machine.
    Root-relative and loopback-absolute redirects both become the gateway path; a redirect to
    any other host (an OAuth provider) is the app's business and passes unchanged.
    """
    prefix = f"/v1/previews/{preview_id}/web"
    # READ IT AS THE BROWSER WILL (the "preview gateway open redirect"). A browser strips C0
    # controls and spaces, drops tabs/newlines anywhere, and reads `\` as `/`, so `\/evil.com`,
    # `/\evil.com` and `//evil.com` are all another host. Classified as written they slipped
    # past both branches below and left the gateway as a redirect to anywhere, or were glued
    # under the prefix as a path. Normalise first, then classify, then emit the normal form.
    location = re.sub(r"[\t\n\r]", "", location).strip("\x00\x01\x02\x03\x04\x05\x06\x07\x08"
                                                        "\x0b\x0c\x0e\x0f\x10\x11\x12\x13"
                                                        "\x14\x15\x16\x17\x18\x19\x1a\x1b"
                                                        "\x1c\x1d\x1e\x1f ")
    scheme_match = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):", location)
    if scheme_match is None or scheme_match.group(1).lower() in ("http", "https"):
        location = location.replace("\\", "/")
    scheme_relative = location.startswith("//")
    if location.startswith("/") and not scheme_relative:
        return f"{prefix}{location}"
    # Scheme-relative is another host: judged as one (and kept scheme-relative if it passes).
    parsed = urllib.parse.urlsplit("http:" + location if scheme_relative else location)
    if parsed.scheme.lower() not in ("http", "https"):
        if not parsed.scheme:
            return location   # `login`, `../x`: resolved against the gateway URL it came from
        # javascript:, data:, file:… never a place a preview may send the browser.
        return f"{prefix}/"
    if _is_loopback_url(parsed.geturl()) and not _is_boss_oidc(parsed):
        rest = parsed.path or "/"
        return f"{prefix}{rest}" + (f"?{parsed.query}" if parsed.query else "") + (
            f"#{parsed.fragment}" if parsed.fragment else "")
    return location


def smoke_test(
    engine,
    preview_id: str,
    checks: Optional[list[dict]] = None,
    settings: Optional[Settings] = None,
) -> dict:
    """Tier-3 smoke canary: hit the live preview URL with a set of checks and assert each — proves
    the deployed app actually works, not just that it serves a page. Records a receipt event.
    Each check: {method, path, body?, expect_status?, expect_contains?}. Default: GET / -> 2xx."""
    import urllib.request
    with begin(engine) as c:
        row = c.execute(text("SELECT project_id,url,status,run_id FROM app_previews WHERE id=:id"),
                        {"id": preview_id}).mappings().first()
    if row is None:
        raise ValueError("preview not found")
    if row["status"] != "running" or not row["url"]:
        raise ValueError(f"preview not running (status={row['status']})")
    base = row["url"].rstrip("/")
    checks = checks or [{"method": "GET", "path": "/", "expect_status": 200}]
    results, passed = [], 0
    for chk in checks:
        method = (chk.get("method") or "GET").upper()
        path = chk.get("path") or "/"
        if method not in {"GET", "HEAD"} or not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ValueError("preview smoke supports only GET/HEAD on relative paths")
        want_status = chk.get("expect_status")
        want_contains = chk.get("expect_contains")
        data = json.dumps(chk["body"]).encode() if chk.get("body") is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        ok, status, detail = False, None, ""
        try:
            resp = urllib.request.urlopen(req, timeout=8)
            status = resp.status
            text_body = resp.read(8000).decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            status = e.code
            text_body = (e.read(2000).decode("utf-8", "ignore") if hasattr(e, "read") else "")
        except Exception as e:  # noqa: BLE001
            detail = str(e)[:160]
            text_body = ""
        if status is not None:
            ok = ((want_status is None or status == want_status)
                  and (want_contains is None or want_contains in text_body))
            if not ok and not detail:
                detail = (f"status {status}" + (f" != {want_status}" if want_status and status != want_status else "")
                          + ("; missing expected text" if want_contains and want_contains not in text_body else ""))
        results.append({"method": method, "path": path, "status": status, "ok": ok, "detail": detail})
        if ok:
            passed += 1
    all_ok = passed == len(checks)
    with begin(engine) as c:
        emit_event(c, row["project_id"], "app_preview.smoke", "operator", "app_preview", preview_id,
                   {"run_id": row["run_id"], "passed": passed, "total": len(checks),
                    "ok": all_ok, "results": results})
    return {
        "preview_id": preview_id,
        "url": operator_preview_open_url(preview_id, settings=settings),
        "internal_url": base,
        "open_url": operator_preview_open_url(preview_id, settings=settings),
        "ok": all_ok,
        "passed": passed,
        "total": len(checks),
        "results": results,
    }


def _teardown(row, settings: Optional[Settings] = None) -> None:
    # Its own origin goes first (BOSSAPP-860; a no-op unless BOSS_PREVIEW_EXPOSE is on), so
    # nothing outside keeps reaching a port about to be reused.
    from . import preview_expose

    preview_expose.detach(str(row.get("id") or ""))
    if row.get("mode") == "docker":
        _docker_rm(row.get("container_id"))
        if row.get("id"):
            _docker_rmi(preview_image_tag(str(row["id"])))
    elif row.get("mode") == "rootfs":
        sandbox_id = row.get("sandbox_task_id")
        if settings and sandbox_id:
            # Explicitly stop/release the retained rootfs when the preview is stopped
            # or expires. Both operations are best-effort and bounded by service TTL.
            base = (settings.sandbox_agent_url or "").rstrip("/")
            headers = ({"X-Sandbox-Token": settings.sandbox_token}
                       if settings.sandbox_token else {})
            try:
                import httpx
                with httpx.Client(timeout=15.0) as client:
                    client.delete(
                        f"{base}/v1/sandbox/tasks/{sandbox_id}/preview", headers=headers
                    )
            except Exception:  # noqa: BLE001
                pass
            MicroVMProvider(settings).release(sandbox_id)
    else:
        _kill(row.get("pid"))
        # The preview's uid: every process it left is killed, its tree removed, the uid freed.
        _release_preview_sandbox(row.get("id"))


def stop_preview(engine, preview_id: str, settings: Optional[Settings] = None) -> dict:
    with begin(engine) as c:
        row = c.execute(text(
            "SELECT id,pid,mode,container_id,sandbox_task_id FROM app_previews WHERE id=:id"
        ),
                        {"id": preview_id}).mappings().first()
        if row is None:
            raise ValueError("preview not found")
        _teardown(dict(row), settings)
        c.execute(text("UPDATE app_previews SET status='stopped' WHERE id=:id"), {"id": preview_id})
    # Its link passes die with it (the gateway also refuses a stopped preview on every request).
    try:
        from ..app_links import service as _links

        with begin(engine) as c:
            _links.revoke_passes_of(c, "preview", preview_id)
    except Exception:  # noqa: BLE001 — a schema without links has no passes to revoke
        pass
    # Its Sign in with BOSS redirect URI goes with it.
    try:
        from ..oidc_provider.app_sign_in import preview_stopped

        with begin(engine) as c:
            run_id = c.execute(text("SELECT run_id, project_id FROM app_previews WHERE id = :id"),
                               {"id": preview_id}).first()
            if run_id is not None:
                preview_stopped(c, project_id=run_id[1], run_id=run_id[0], preview_id=preview_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("preview %s: its sign-in redirect was not removed (%s)", preview_id, type(exc).__name__)
    return {"id": preview_id, "status": "stopped"}


def _attempt_chain(mode: str):
    """Which substrates to try, in order. `auto` prefers isolation and falls back."""
    if mode == "process":
        return [start_preview]
    if mode == "docker":
        return [start_container_preview]
    if mode == "rootfs":
        return [start_rootfs_preview]
    return [start_rootfs_preview, start_container_preview, start_preview]


#: What each substrate is called when it is being tried, so the ledger reads as English.
_SUBSTRATE_LABELS = {
    "start_rootfs_preview": "Reuse the build's retained rootfs",
    "start_container_preview": "Build and run a container",
    "start_preview": "Run as a managed host process",
}


def run_preview_attempts(engine, settings: Settings, run_id: str, *,
                         ttl_minutes: int = 60, mode: str = "auto",
                         steps=None) -> dict:
    """Try each substrate until one serves. Raises the domain errors unchanged.

    `steps` is an optional StepRecorder. `auto` tries three substrates in order, and when it
    ended in "preview could not start" there was no way to tell whether that meant one thing
    was tried and failed or three were — so each attempt records itself, with the reason it
    was passed over.
    """
    last: Optional[dict] = None
    for fn in _attempt_chain(mode):
        name = getattr(fn, "__name__", "attempt")
        label = _SUBSTRATE_LABELS.get(name, name)
        if steps is None:
            try:
                res = fn(engine, settings, run_id, ttl_minutes=ttl_minutes)
            except RuntimeError as exc:
                last = {"status": "failed", "error": str(exc)}
                continue
            if res.get("status") == "running":
                return res
            last = res
            continue
        with steps.step(name, label=label) as st:
            try:
                res = fn(engine, settings, run_id, ttl_minutes=ttl_minutes)
            except RuntimeError as exc:
                st.failed(str(exc)[:400])
                last = {"status": "failed", "error": str(exc)}
                continue
            if res.get("status") == "running":
                st.log(f"serving at {res.get('url') or 'a local URL'}")
                return res
            # Not a crash — this substrate simply could not serve, and the NEXT one may. That
            # is a skip with a reason, not a failure.
            st.skip((res.get("error") or res.get("failure_kind") or "did not serve")[:400])
            last = res
    return last or {"status": "failed", "error": "preview could not start"}


def start_preview_async(engine, settings: Settings, run_id: str, *,
                        ttl_minutes: int = 60, mode: str = "auto",
                        actor: str = "operator") -> dict:
    """Begin a preview and RETURN, leaving the build to finish in the background.

    Starting a preview clones, installs dependencies, builds and launches — minutes of work,
    and it was done inside the request. The console gives up after 120s, so the operator was
    shown "the control plane did not answer within 120s" under a heading reading "Preview /
    tests failed", while the build was still running and would often go on to succeed. Worse,
    until the attempt finished there was NO ROW AT ALL: nothing in the previews list, nothing
    in the Activity Log, nothing to poll. The operator was blind by construction.

    So a row exists from the first moment, in the `starting` state the schema already
    documented and nothing used. The console polls previews already; now there is something to
    poll. Exactly one row per attempt: whatever the substrate creates is folded back onto this
    id and its own row removed, so the id handed out here is the id that ends up terminal.
    """
    with begin(engine) as c:
        run = c.execute(text(
            "SELECT project_id, pull_request_url FROM code_task_runs WHERE id=:r"),
            {"r": run_id}).mappings().first()
    if not run:
        raise ValueError(f"code task run not found: {run_id}")

    preview_id = make_id("prev")
    project_id = run["project_id"]
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO app_previews (id,project_id,run_id,status,mode) "
            "VALUES (:id,:p,:r,'starting',:m)"),
            {"id": preview_id, "p": project_id, "r": run_id,
             "m": mode if mode in ("process", "docker", "rootfs") else "process"})
        emit_event(c, project_id, "app_preview.starting", actor, "app_preview", preview_id,
                   {"run_id": run_id, "mode": mode, "ttl_minutes": ttl_minutes})

    def _finish() -> None:
        from .deployment_steps import StepRecorder

        steps = StepRecorder(engine, preview_id, table="app_previews", set_duration=False)
        try:
            res = run_preview_attempts(engine, settings, run_id,
                                       ttl_minutes=ttl_minutes, mode=mode, steps=steps)
            child = res.get("id")
            with begin(engine) as c:
                if child and child != preview_id:
                    # Fold the substrate's row onto the id already handed out, then drop it, so
                    # a caller polling that id sees the outcome instead of a row stuck at
                    # 'starting' beside a second row it never heard about.
                    c.execute(text(
                        "UPDATE app_previews AS t SET status=s.status, url=s.url, port=s.port, "
                        "pid=s.pid, command=s.command, repo=s.repo, branch=s.branch, "
                        "mode=s.mode, substrate=s.substrate, build_substrate=s.build_substrate, "
                        "container_id=s.container_id, sandbox_task_id=s.sandbox_task_id, "
                        "error=s.error, failure_kind=s.failure_kind, log_path=s.log_path, "
                        "expires_at=s.expires_at "
                        "FROM app_previews AS s WHERE t.id=:tid AND s.id=:sid"),
                        {"tid": preview_id, "sid": child})
                    c.execute(text("DELETE FROM app_previews WHERE id=:sid"), {"sid": child})
                else:
                    status = res.get("status") or "failed"
                    error = (res.get("error") or "")[:500] or None
                    c.execute(text(
                        "UPDATE app_previews SET status=:st, error=:e, "
                        "command=COALESCE(:cmd, command), "
                        "failure_kind=COALESCE(:fk, failure_kind) WHERE id=:id"),
                        {"st": status, "e": error, "id": preview_id,
                         "cmd": res.get("command"), "fk": res.get("failure_kind")})
                    # A FAILURE THAT IS RETURNED IS STILL A FAILURE.
                    #
                    # The exception path below emits app_preview.failed; this one — the
                    # ordinary return carrying status='failed' — emitted nothing. That is the
                    # path a build error takes: "port_timeout: app did not serve HTTP within
                    # timeout … Could not resolve entry module index.html" was written to the
                    # preview row and to no event, so an operator who went to the Activity log
                    # to find out why found an empty screen and concluded the error had been
                    # swallowed. It had — by the log, not by the preview.
                    if status == "failed":
                        emit_event(c, project_id, "app_preview.failed", actor, "app_preview",
                                   preview_id,
                                   {"run_id": run_id,
                                    "failure_kind": res.get("failure_kind") or "start_failed",
                                    "error": error or ""})
        except Exception as exc:  # noqa: BLE001 — the thread must never die silently
            kind = "ambiguous_app" if isinstance(exc, AmbiguousAppError) else (
                "not_ready" if isinstance(exc, PreviewNotReady) else "start_crashed")
            try:
                with begin(engine) as c:
                    c.execute(text(
                        "UPDATE app_previews SET status='failed', error=:e, failure_kind=:k "
                        "WHERE id=:id"),
                        {"e": str(exc)[:500], "k": kind, "id": preview_id})
                    emit_event(c, project_id, "app_preview.failed", actor, "app_preview",
                               preview_id, {"run_id": run_id, "failure_kind": kind,
                                            "error": str(exc)[:500]})
            except Exception:  # noqa: BLE001
                logger.exception("preview %s failed and could not be recorded", preview_id)

        # THE APP'S OWN OUTPUT, not only BOSS's record of it.
        #
        # Everything above writes what BOSS observed: a status, a failure_kind, a 500-char
        # error. The container's actual log — the stack trace, the missing module, the port it
        # tried to bind — stayed on the preview host behind `read_preview_log`, reachable by an
        # agent with a prev_ id and by no one reading the Activity log. This copies the tail
        # into app_logs so it lands on that screen next to the step that caused it.
        #
        # Outside the try/except above ON PURPOSE: it must run whether the preview succeeded or
        # crashed, and a failed build is exactly the case whose log is worth having.
        try:
            from .preview_log_ingest import ingest_preview_log

            ingest_preview_log(engine, preview_id, project_id=project_id, run_id=run_id)
        except Exception:  # noqa: BLE001 — never let log capture break the preview
            logger.debug("preview %s: log ingest failed", preview_id, exc_info=True)

    threading.Thread(target=_finish, name=f"preview-{preview_id}", daemon=True).start()
    return {"id": preview_id, "status": "starting", "run_id": run_id,
            "message": "Preview is building. It appears in the previews list as it progresses."}


def list_previews(
    engine, project_id: str, limit: int = 50, settings: Optional[Settings] = None
) -> list[dict]:
    """Reap expired previews (tear down, mark, and eventually forget) and return recent ones."""
    with begin(engine) as c:
        expired = c.execute(text(
            "SELECT id,pid,mode,container_id,sandbox_task_id FROM app_previews "
            "WHERE status='running' AND expires_at < now()")).mappings().all()
        for r in expired:
            _teardown(dict(r), settings)
            c.execute(text("UPDATE app_previews SET status='expired' WHERE id=:id"), {"id": r["id"]})

        # AND THEN FORGET THE OLD ONES.
        #
        # Expiring marked rows but never removed them, so `app_previews` only grew. Measured on
        # a live deployment: 50 rows, of which 47 expired, 1 failed, 1 stopped and 1 running.
        #
        # That is not merely untidy. This query returns the newest N rows whatever their status
        # and callers filter for live ones afterwards, so a window that is 94% dead can push a
        # genuinely running preview for an older app out of it entirely — the preview exists,
        # and the app's card cannot see it.
        #
        # Seven days, and only for previews that are already finished. A preview's life is an
        # hour; a week-old expired row has no reader. The run history it belonged to lives in
        # code_task_runs and is untouched.
        c.execute(text(
            "DELETE FROM app_previews "
            "WHERE status IN ('expired','stopped','failed') "
            "  AND COALESCE(expires_at, created_at) < now() - interval '7 days'"))

        rows = c.execute(text(
            "SELECT id,run_id,repo,branch,url,port,mode,substrate,build_substrate,sandbox_task_id,"
            "status,error,failure_kind,expires_at,created_at,steps,demo_logins "
            "FROM app_previews WHERE project_id=:p ORDER BY created_at DESC LIMIT :l"),
            {"p": project_id, "l": int(limit)}).mappings().all()
    # Publicize at the boundary: DB keeps loopback for the proxy; callers get open_url.
    return [publicize_preview(dict(r), settings=settings) for r in rows]


def start_preview_for_run(engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60,
                          ready_timeout: Optional[float] = None) -> dict:
    """Start a preview the same way the console's button does: try the substrates in order.

    Exists so the pipeline's auto-preview and the API endpoint share ONE launch policy. The
    endpoint used to own the ordering inline, and anything else calling a single start_* would
    have silently used a different strategy — the same drift that made the container path pick
    a different app from the host path.
    """
    attempts = (start_rootfs_preview, start_container_preview, start_preview)
    last: dict = {"status": "failed", "error": "preview could not start"}
    for fn in attempts:
        try:
            # Only the host launch waits on the app for long; the others bound their own waits.
            bound = ({"ready_timeout": ready_timeout}
                     if fn is start_preview and ready_timeout else {})
            res = fn(engine, settings, run_id, ttl_minutes=ttl_minutes, **bound)
        except (PreviewNotReady, AmbiguousAppError):
            raise
        except (ValueError, RuntimeError) as exc:
            last = {"status": "failed", "error": str(exc)}
            continue
        if res.get("status") == "running":
            return res
        last = res
    return last


# --------------------------------------------------------------------------- #
# Reclaiming preview workdirs
# --------------------------------------------------------------------------- #
#: Terminal preview states. A preview in one of these can never serve again, so its tree is
#: dead weight — but note `running` is deliberately absent: that process is serving FROM its
#: workdir and deleting it pulls the floor out from under a live preview.
_TERMINAL_PREVIEW_STATES = ("failed", "stopped", "expired")

#: How long an unrecognised directory must sit before it is reclaimed. A workdir is created
#: BEFORE its row is inserted, so a young orphan may simply be a preview mid-start.
ORPHAN_WORKDIR_AFTER_HOURS = 6


def _preview_log_archive(preview_id: str) -> Path:
    """Where a reclaimed preview's log is kept.

    The log lives INSIDE the workdir (`workdir/_preview.log`), so deleting the tree destroys the
    evidence — and that log is exactly what identified `MODULE_NOT_FOUND` three levels down when
    a finished app could not be previewed. Keep the log, drop the 300 MB of node_modules around
    it: the diagnosis is a few KB, the waste is everything else.
    """
    return PREVIEW_ROOT / "_logs" / f"{preview_id}.log"


def reclaim_preview_workdir(preview_id: str) -> int:
    """Delete a terminal preview's tree, preserving its log. Returns bytes reclaimed.

    Never raises: reclaiming space must not be able to fail a caller that was doing something
    else, and a directory that has already gone is a success, not an error.
    """
    freed = 0
    try:
        if not PREVIEW_ROOT.exists():
            return 0
        archive = _preview_log_archive(preview_id)
        for d in PREVIEW_ROOT.glob(f"{preview_id}-*"):
            if not d.is_dir():
                continue
            try:
                log = d / "_preview.log"
                if log.is_file() and not archive.exists():
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(log, archive)
            except Exception:  # noqa: BLE001
                pass  # a lost log is regrettable; a full disk is an outage
            try:
                for root, _dirs, files in os.walk(d):
                    for f in files:
                        try:
                            freed += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
            except Exception:  # noqa: BLE001
                pass
            shutil.rmtree(d, ignore_errors=True)
    except Exception:  # noqa: BLE001
        logger.debug("reclaim_preview_workdir_failed preview=%s", preview_id, exc_info=True)
    return freed


def sweep_preview_workdirs(engine, *, dry_run: bool = False) -> dict:
    """Reclaim the tree of every preview that can no longer serve.

    THE SWEEP IS THE MECHANISM, not a backstop. `start_preview` has several launch paths, each
    with its own `_fail` closure, and cleanup wired into call sites is precisely how the same
    rule ends up expressed in four places and satisfied in three (S3). This joins the FILESYSTEM
    against the DATABASE, so a path that forgets to call anything is still covered — including
    one that does not exist yet.

    Two populations:
      * previews in a terminal state that still have a tree;
      * directories with no row at all — a process that died between mkdtemp and INSERT. Those
        are reclaimed only after a grace period, because a workdir is created BEFORE its row.
    """
    import time as _time

    if not PREVIEW_ROOT.exists():
        return {"reclaimed": 0, "bytes": 0, "orphans": 0, "dry_run": dry_run}

    known: dict[str, str] = {}
    try:
        with begin(engine) as c:
            for r in c.execute(text("SELECT id, status FROM app_previews")).mappings():
                known[str(r["id"])] = str(r["status"] or "")
    except Exception:  # noqa: BLE001
        logger.debug("sweep_preview_workdirs_db_failed", exc_info=True)
        return {"reclaimed": 0, "bytes": 0, "orphans": 0, "error": "db unavailable"}

    reclaimed, orphans, freed = [], 0, 0
    cutoff = _time.time() - ORPHAN_WORKDIR_AFTER_HOURS * 3600
    for d in PREVIEW_ROOT.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue  # _logs is ours
        preview_id = d.name.rsplit("-", 1)[0] if "-" in d.name else d.name
        status = known.get(preview_id)
        if status is None:
            try:
                if d.stat().st_mtime > cutoff:
                    continue  # may be starting right now
            except OSError:
                continue
            orphans += 1
        elif status not in _TERMINAL_PREVIEW_STATES:
            continue  # running, or starting — its process is serving from this tree
        if not dry_run:
            freed += reclaim_preview_workdir(preview_id)
        reclaimed.append(preview_id)

    if reclaimed and not dry_run:
        logger.info("preview_workdirs_reclaimed count=%d orphans=%d mb=%.1f",
                    len(reclaimed), orphans, freed / 1_000_000)
    return {"reclaimed": len(reclaimed), "bytes": freed, "orphans": orphans,
            "ids": reclaimed[:10], "dry_run": dry_run}
