#!/usr/bin/env python3
"""Community GA bar: working software through the factory.

Not a control-plane health check. Pass means an operator can open a
non-fixture preview of a completed code.task whose tests passed.

  make qa-working-software          # live if BOSS_API_KEY is set
  make qa-working-software DRY_RUN=1

Never prints BOSS_API_KEY. Never launches a new budgeted code.task.
Starting a preview from an already-completed run is allowed (no LLM).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.qa.lib import (  # noqa: E402
    DEFAULT_API_URL,
    DEFAULT_PROJECT_ID,
    CheckResult,
    env_flag,
    is_2xx,
    redact_secrets,
    summarize,
)

GATE_NAME = "working_software_gate"
GATE_VERSION = "1.0.0"
DEFAULT_ACTOR = "executive_console"
FIXTURE_MARKERS = (
    "not a sandboxed app",
    "fixture — not",
    "mode=fixture",
    "preview fixture",
)
APP_MARKERS = (
    "bugbox",
    "approveit",
    "hookwatch",
    'id="root"',
    "id='root'",
)


@dataclass
class GateReport:
    gate: str = GATE_NAME
    version: str = GATE_VERSION
    ok: bool = False
    dry_run: bool = False
    started_at: str = ""
    finished_at: str = ""
    api_base: str = ""
    project_id: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _auth_headers(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    return {
        "Authorization": f"Bearer {api_key}",
        "X-Boss-Api-Key": api_key,
    }


def api_request(
    method: str,
    api_base: str,
    path: str,
    *,
    api_key: str | None,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
    accept_html: bool = False,
) -> tuple[int, Any]:
    from urllib.parse import urlencode

    url = f"{api_base.rstrip('/')}/{path.lstrip('/')}"
    if query:
        filtered = {k: v for k, v in query.items() if v is not None}
        if filtered:
            url = f"{url}?{urlencode(filtered)}"
    headers = {"Accept": "text/html,application/json" if accept_html else "application/json"}
    headers.update(_auth_headers(api_key))
    encoded = None
    if body is not None:
        encoded = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=encoded, headers=headers, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
            status = int(response.status)
            ctype = response.headers.get("content-type") or ""
    except HTTPError as error:
        raw = error.read()
        status = int(error.code)
        ctype = error.headers.get("content-type") if error.headers else ""
    except URLError as error:
        raise RuntimeError(
            redact_secrets(
                f"BOSS API {method} {path} unreachable: {error.reason}",
                [api_key] if api_key else None,
            )
        ) from error
    text = raw.decode("utf-8", errors="replace")
    if accept_html:
        return status, {"content_type": ctype, "text": text, "bytes": len(raw)}
    try:
        return status, json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return status, {"raw": text[:500]}


def run_has_passing_verify(run: dict[str, Any]) -> bool:
    state = run.get("state") or {}
    if state.get("tests_passed") is True:
        return True
    if state.get("test_exit_code") == 0 and "verify" in str(state.get("test_command") or "").lower():
        return True
    return False


def is_app_run(run: dict[str, Any]) -> bool:
    if run.get("status") != "completed":
        return False
    if not run_has_passing_verify(run):
        return False
    fctx = run.get("finalize_ctx") or {}
    if fctx.get("web_app_build") is True:
        return True
    state = run.get("state") or {}
    files = state.get("impl_files") or []
    if any(str(p).endswith((".tsx", ".html", ".css", ".jsx")) for p in files):
        return True
    title = (run.get("title") or "").lower()
    return any(name in title for name in ("bugbox", "approveit", "hookwatch", "web app"))


def pick_working_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    apps = [r for r in runs if is_app_run(r)]
    if not apps:
        return None
    return apps[0]


def is_fixture_html(text: str) -> bool:
    blob = (text or "").lower()
    return any(marker in blob for marker in FIXTURE_MARKERS)


def is_working_app_html(text: str) -> bool:
    blob = (text or "").lower()
    if is_fixture_html(blob):
        return False
    return any(marker in blob for marker in APP_MARKERS)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--actor", default=DEFAULT_ACTOR)
    parser.add_argument("--output", default=None)
    parser.add_argument("--start-preview", action="store_true", default=False)
    parser.add_argument(
        "--reuse-only",
        action="store_true",
        help="Do not POST /preview; require an already-running non-fixture preview.",
    )
    return parser.parse_args(argv)


def run_gate(args: argparse.Namespace) -> GateReport:
    started = datetime.now(timezone.utc).isoformat()
    api_base = (
        (args.api_base or "").strip()
        or os.environ.get("BOSS_API_BASE", "").strip()
        or os.environ.get("BOSS_API_URL", "").strip()
        or DEFAULT_API_URL
    )
    project_id = (
        (args.project_id or "").strip()
        or os.environ.get("PROJECT_ID", "").strip()
        or os.environ.get("BOSS_PROJECT_ID", "").strip()
        or DEFAULT_PROJECT_ID
    )
    api_key = os.environ.get("BOSS_API_KEY", "").strip() or None
    dry = bool(args.dry_run or env_flag("DRY_RUN") or not api_key)
    checks: list[CheckResult] = []
    evidence: dict[str, Any] = {
        "bar": (
            "Community GA requires a completed factory app with passing "
            "verify plus a reachable non-fixture preview."
        )
    }

    if dry:
        checks.append(
            CheckResult(
                name="structure.bar_defined",
                ok=True,
                detail="GA bar is working software, not qa-live 10/10 alone",
            )
        )
        checks.append(
            CheckResult(
                name="structure.preview_route",
                ok=True,
                detail="POST /v1/code-tasks/{id}/preview + GET /v1/previews/{id}/web/",
            )
        )
        checks.append(
            CheckResult(
                name="structure.no_new_spend",
                ok=True,
                detail="Gate never POSTs a new code.task / canary launch",
            )
        )
    else:
        status, wip = api_request(
            "GET", api_base, "/v1/factory/wip", api_key=api_key, query={"project_id": project_id}
        )
        checks.append(
            CheckResult(
                name="factory.wip",
                ok=is_2xx(status),
                detail=f"status={status} substrate={wip.get('coding_substrate') if isinstance(wip, dict) else None}",
                status_code=status,
                evidence={"coding_substrate": (wip or {}).get("coding_substrate") if isinstance(wip, dict) else None},
            )
        )

        status, payload = api_request(
            "GET",
            api_base,
            "/v1/code-tasks",
            api_key=api_key,
            query={"project_id": project_id, "limit": 50},
        )
        runs = []
        if isinstance(payload, dict):
            runs = payload.get("code_tasks") or payload.get("items") or []
        picked = pick_working_run(runs if isinstance(runs, list) else [])
        evidence["picked_run_id"] = (picked or {}).get("id")
        evidence["picked_title"] = ((picked or {}).get("title") or "")[:120]
        evidence["picked_pr"] = (picked or {}).get("pull_request_url")
        checks.append(
            CheckResult(
                name="factory.completed_app",
                ok=picked is not None,
                required=True,
                detail=(
                    f"picked {(picked or {}).get('id')} tests_passed={run_has_passing_verify(picked or {})}"
                    if picked
                    else "no completed code.task with passing verify / web app files"
                ),
                status_code=status,
            )
        )

        status, plist = api_request(
            "GET",
            api_base,
            "/v1/previews",
            api_key=api_key,
            query={"project_id": project_id, "limit": 50},
        )
        previews = (plist or {}).get("previews") if isinstance(plist, dict) else []
        running = [
            p
            for p in (previews or [])
            if isinstance(p, dict)
            and p.get("status") == "running"
            and p.get("mode") != "fixture"
        ]
        preview = running[0] if running else None
        if preview is None and picked and not args.reuse_only:
            pstatus, started_preview = api_request(
                "POST",
                api_base,
                f"/v1/code-tasks/{picked['id']}/preview",
                api_key=api_key,
                body={"actor": args.actor, "mode": "process", "ttl_minutes": 60},
                timeout=60.0,
            )
            if is_2xx(pstatus) and isinstance(started_preview, dict):
                preview = started_preview
                evidence["preview_started"] = True
            checks.append(
                CheckResult(
                    name="preview.start",
                    ok=is_2xx(pstatus) and isinstance(started_preview, dict) and started_preview.get("status") == "running",
                    detail=f"status={pstatus} id={(started_preview or {}).get('id')}",
                    status_code=pstatus,
                )
            )
        elif preview is None:
            checks.append(
                CheckResult(
                    name="preview.running",
                    ok=False,
                    detail="no running non-fixture preview (--reuse-only or no completed app)",
                )
            )

        if preview:
            pid = preview.get("id")
            open_url = preview.get("open_url") or preview.get("url") or ""
            evidence["preview_id"] = pid
            evidence["open_url"] = open_url
            loopback = any(h in (open_url or "") for h in ("127.0.0.1", "localhost", "[::1]"))
            checks.append(
                CheckResult(
                    name="preview.public_url",
                    ok=bool(open_url) and not loopback,
                    detail=open_url or "missing url",
                )
            )
            hstatus, html = api_request(
                "GET",
                api_base,
                f"/v1/previews/{pid}/web/",
                api_key=api_key,
                accept_html=True,
                timeout=20.0,
            )
            text = (html or {}).get("text") or ""
            fixture = is_fixture_html(text)
            working = is_working_app_html(text)
            evidence["html_bytes"] = (html or {}).get("bytes")
            evidence["html_fixture"] = fixture
            evidence["html_working_app"] = working
            title = ""
            lower = text.lower()
            if "<title>" in lower:
                start = lower.find("<title>") + 7
                title = text[start : start + 80].split("<")[0]
            evidence["html_title"] = title
            checks.append(
                CheckResult(
                    name="preview.html_working_app",
                    ok=is_2xx(hstatus) and working and not fixture,
                    required=True,
                    detail=f"http={hstatus} title={title!r} fixture={fixture} working={working}",
                    status_code=hstatus,
                )
            )

    summary = summarize(checks)
    finished = datetime.now(timezone.utc).isoformat()
    return GateReport(
        ok=bool(summary.get("ok")),
        dry_run=dry,
        started_at=started,
        finished_at=finished,
        api_base=api_base,
        project_id=project_id,
        checks=[c.to_dict() for c in checks],
        summary=summary,
        evidence=evidence,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = run_gate(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    payload = report.to_dict()
    text = json.dumps(payload, indent=2)
    text = redact_secrets(text, [os.environ.get("BOSS_API_KEY", "")])
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    print(
        f"{GATE_NAME}: {'PASS' if report.ok else 'FAIL'} "
        f"required {report.summary.get('required_passed')}/{report.summary.get('required')}"
        + (" (dry-run)" if report.dry_run else ""),
        file=sys.stderr,
    )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
