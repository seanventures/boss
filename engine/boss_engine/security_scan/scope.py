"""WHAT A SCAN MAY TOUCH (spec §4.2, §4.7 S4.1). Never an arbitrary target: SSRF and abuse.

* The SOURCE is the app's own code: the app (`app_key`, the stable app id builds stamp on their
  runs) must have a build in THIS project, and `ref` is that app's branch or a commit sha on it.
  No repository, branch or URL is ever taken from the request.
* The DAST TARGET is one of:
    - `sandbox`  the app started inside the scan's own sandbox, on loopback (the default);
    - `preview:<id>`  a running preview of the SAME project AND app, served on this BOSS's
      loopback (a preview on another host needs an F5 link, which this stream does not build).
  A URL, a host, a deploy target, another project's or another app's preview: refused.
* ACTIVE scanning (ZAP API scan) only when the request asks for it (D12: off by default), and
  only against those same two targets — there is no third.

The sandbox's network fence (scripts/sandbox/entrypoint.sh) is the second wall: the scanners run as
the run's uid, which reaches loopback, the API and boss-egress only.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy import text

from .findings import TOOLS

ZAP_MODES = ("baseline", "api")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_APP_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PREVIEW_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class ScopeRefused(ValueError):
    """The request names something a scan may not touch. `code` is the API's error word."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class AppSource:
    project_id: str
    app_key: str
    run_id: str
    owner: str
    repo: str
    branch: str
    pull_request_url: str = ""
    #: the ref to check out: the branch head (None) or a commit sha on it
    commit: Optional[str] = None


@dataclass
class ScanRequest:
    tools: tuple[str, ...]
    zap_mode: str = "baseline"
    zap_target: str = "sandbox"
    #: `preview:<id>` resolved: {"kind": "preview", "preview_id", "url"}; or {"kind": "sandbox"}
    target: dict[str, Any] = field(default_factory=lambda: {"kind": "sandbox"})


def _loopback(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_app(conn: Any, project_id: str, app_key: str, ref: Optional[str], settings: Any = None) -> AppSource:
    if not app_key or not _APP_KEY.match(str(app_key)):
        raise ScopeRefused("invalid_app_key", "app_key is required and must be an app id")
    row = conn.execute(text(
        "SELECT id, spec, finalize_ctx, pull_request_url FROM code_task_runs "
        "WHERE project_id = :p AND finalize_ctx->>'app_id' = :a "
        "ORDER BY created_at DESC LIMIT 1"), {"p": project_id, "a": app_key}).mappings().first()
    if row is None:
        # Named by another of its handles (its name in any case, its slug, a run id): the scan
        # is filed under the app's stable id, whichever one the person held.
        from ..app_links.service import resolve_app_id

        resolved = resolve_app_id(conn, project_id, app_key)
        if resolved and resolved != app_key:
            return resolve_app(conn, project_id, resolved, ref, settings)
        raise ScopeRefused("app_not_found", f"no app {app_key} in this project", status=404)
    spec = row["spec"] or {}
    fctx = row["finalize_ctx"] or {}
    owner = str(spec.get("owner") or getattr(settings, "target_github_owner", "") or "")
    repo = str(spec.get("repo") or getattr(settings, "target_github_repo", "") or "")
    # The app's branch, as the preview resolves it: iterations share the ROOT run's branch.
    branch = str(fctx.get("work_branch") or f"boss/code/{fctx.get('root_run_id') or row['id']}")
    src = AppSource(project_id=project_id, app_key=app_key, run_id=str(row["id"]), owner=owner,
                    repo=repo, branch=branch, pull_request_url=str(row["pull_request_url"] or ""))
    if ref in (None, "", branch):
        return src
    ref = str(ref).strip().lower()
    if _SHA.match(ref):
        src.commit = ref            # checked against the branch's history at checkout
        return src
    raise ScopeRefused("ref_out_of_scope",
                       "ref must be the app's branch or a commit sha on it; another branch, a tag "
                       "or a URL is not scanned")


def resolve_request(conn: Any, project_id: str, app_key: str, *, tools: Any = None,
                    zap: Optional[dict[str, Any]] = None) -> ScanRequest:
    asked = tuple(tools) if tools else TOOLS
    if not all(isinstance(t, str) for t in asked):
        raise ScopeRefused("invalid_tools", "tools must be names")
    unknown = sorted(set(asked) - set(TOOLS))
    if unknown:
        raise ScopeRefused("unknown_tool", f"unknown tools: {', '.join(unknown)}; known: {', '.join(TOOLS)}")
    ordered = tuple(t for t in TOOLS if t in asked)        # semgrep, osv, zap: the run order
    zap = dict(zap or {})
    extra = sorted(set(zap) - {"mode", "target"})
    if extra:
        raise ScopeRefused("invalid_zap_options", f"unknown zap options: {', '.join(extra)}")
    mode = str(zap.get("mode") or "baseline")
    if mode not in ZAP_MODES:
        raise ScopeRefused("invalid_zap_mode", "zap.mode is baseline or api")
    target = zap.get("target") or "sandbox"
    if not isinstance(target, str):
        raise ScopeRefused("target_out_of_scope", "zap.target is `sandbox` or `preview:<id>`")
    req = ScanRequest(tools=ordered, zap_mode=mode, zap_target=target)
    if target == "sandbox":
        return req
    if not target.startswith("preview:") or not _PREVIEW_ID.match(target[len("preview:"):]):
        # A URL, a host name, an IP, a deploy target: never.
        raise ScopeRefused("target_out_of_scope",
                           "a scan target is `sandbox` (the app started in the scan) or "
                           "`preview:<id>` (a preview of this app); a URL is never scanned")
    preview_id = target[len("preview:"):]
    req.target = resolve_preview(conn, project_id, app_key, preview_id)
    return req


def resolve_preview(conn: Any, project_id: str, app_key: str, preview_id: str) -> dict[str, Any]:
    """A RUNNING preview of this project's app, served on this BOSS's loopback."""
    row = conn.execute(text(
        "SELECT ap.url, ap.status, ap.project_id, r.finalize_ctx->>'app_id' AS app_id "
        "FROM app_previews ap LEFT JOIN code_task_runs r ON r.id = ap.run_id "
        "WHERE ap.id = :i"), {"i": preview_id}).mappings().first()
    if row is None:
        row = conn.execute(text(
            "SELECT COALESCE(internal_url, url) AS url, status, project_id, app_id "
            "FROM preview_sandboxes WHERE id = :i"), {"i": preview_id}).mappings().first()
    # Another project's preview is reported exactly like a missing one: no existence oracle.
    if row is None or row["project_id"] != project_id:
        raise ScopeRefused("preview_not_found", "no such preview in this project", status=404)
    if row["app_id"] != app_key:
        raise ScopeRefused("target_out_of_scope", "that preview is of another app")
    if str(row["status"] or "") not in ("running", "ready"):
        raise ScopeRefused("preview_not_running", "that preview is not running", status=409)
    url = str(row["url"] or "")
    if not _loopback(url):
        raise ScopeRefused("preview_not_reachable",
                           "that preview is not served on this BOSS; scanning a preview on another "
                           "host needs a link to that host (App ▸ Links); scan the app in its own sandbox "
                           "instead (zap target `sandbox`)", status=409)
    return {"kind": "preview", "preview_id": preview_id, "url": url}
