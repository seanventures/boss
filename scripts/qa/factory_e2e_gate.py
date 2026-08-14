#!/usr/bin/env python3
"""Factory E2E gate — Community GA software-factory surface check.

Two modes:

  --dry-run (default when BOSS_API_KEY missing)
    Structural validation of factory / code-task endpoint contracts,
    factory quality-loop exports, scorecard v1.1 ui/ux dimensions, and
    the no-budgeted-POST rule. No network. Exit 0 if structure is coherent.

  live (credentials present)
    1. health
    2. GET /v1/factory/wip
    3. GET /v1/code-tasks?project_id=…
    4. Optional POST /v1/code-tasks
         --create-fixture   no-spend simulated=true (only if wip.fixture_supported)
         --create-code-task real governed run (budgeted; needs ALLOW_CREATE)
    5. GET /v1/software-factory/proposals/queue
         404/405 → SKIP (route is POST-only; GET is expected to 405)

Exit codes:
  0  all required checks passed
  1  one or more required checks failed
  2  usage / configuration error

---------------------------------------------------------------------------
Required environment for FULL LIVE run (optional create path)
---------------------------------------------------------------------------

  BOSS_API_BASE or BOSS_API_URL   API base URL
  BOSS_API_KEY                    API key (never printed / never committed)
  PROJECT_ID or BOSS_PROJECT_ID   project scope (default boss-dogfood)

Optional for --create-code-task (queues a real governed run — spends budget):

  BOSS_FACTORY_OWNER or GITHUB_OWNER   GitHub owner for the code task
  BOSS_FACTORY_REPO or GITHUB_REPO     GitHub repo for the code task
  BOSS_FACTORY_BASE_BRANCH             base branch (default main)
  BOSS_FACTORY_ALLOW_CREATE=1          hard confirm create is intentional

--create-fixture (no spend):
  POST /v1/code-tasks with simulated=true. Completes create→persist→GET
  evidence without a model, reservation, or code.task job.
  This is NOT a sealed factory path. There is no deterministic_brief /
  dry-run worker that can finish a real code-task without an LLM.
  Older APIs ignore unknown keys — the gate only POSTs the fixture when
  GET /v1/factory/wip advertises fixture_supported=true.

Without create flags, live mode is read-only (list endpoints only).

Examples:
  python3 scripts/qa/factory_e2e_gate.py --dry-run
  BOSS_API_KEY=… make qa-factory-e2e
  BOSS_API_KEY=… python3 scripts/qa/factory_e2e_gate.py --create-fixture
  BOSS_API_KEY=… BOSS_FACTORY_ALLOW_CREATE=1 \\
    BOSS_FACTORY_OWNER=acme BOSS_FACTORY_REPO=app \\
    python3 scripts/qa/factory_e2e_gate.py --create-code-task
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GATE_NAME = "factory_e2e_gate"
GATE_VERSION = "1.3.0"
REPO_ROOT = Path(__file__).resolve().parents[2]
QUALITY_LOOP_RELPATH = "engine/boss_engine/services/factory_quality_loop.py"
BENCHMARK_APPS_RELPATH = "engine/boss_engine/services/benchmark_apps.py"
QUALITY_LOOP_EXPORTS = (
    "should_run_quality_pass",
    "collect_workspace_evidence",
    "quality_improve_prompt",
)
PROPOSALS_QUEUE_SKIP_STATUSES = frozenset({404, 405})
DEFAULT_API_URL = "https://boss-api.local.sean.network"
DEFAULT_PROJECT_ID = "boss-dogfood"
DEFAULT_TIMEOUT_SECONDS = 30.0
COMMAND_PREFIX = "[factory-e2e-gate]"

# Endpoint contracts validated in dry-run and probed live.
STRUCTURAL_ENDPOINTS: list[dict[str, Any]] = [
    {
        "name": "health",
        "method": "GET",
        "path": "/health",
        "auth": False,
        "required": True,
    },
    {
        "name": "factory.wip",
        "method": "GET",
        "path": "/v1/factory/wip?project_id={project_id}&wip_limit=5",
        "auth": True,
        "required": True,
    },
    {
        "name": "code_tasks.list",
        "method": "GET",
        "path": "/v1/code-tasks?project_id={project_id}&limit=20",
        "auth": True,
        "required": True,
    },
    {
        "name": "code_tasks.create",
        "method": "POST",
        "path": "/v1/code-tasks",
        "auth": True,
        "required": False,  # only required when --create-code-task
        "body_keys": [
            "project_id",
            "title",
            "instructions",
            "owner",
            "repo",
            "base_branch",
        ],
    },
    {
        "name": "code_tasks.create_fixture",
        "method": "POST",
        "path": "/v1/code-tasks",
        "auth": True,
        "required": False,  # only when --create-fixture and fixture_supported
        "body_keys": [
            "project_id",
            "title",
            "instructions",
            "owner",
            "repo",
            "base_branch",
            "simulated",
        ],
        "note": "simulated=true no-spend fixture; not a sealed factory path",
    },
    {
        "name": "factory.proposals_queue",
        "method": "GET",
        "path": "/v1/software-factory/proposals/queue?project_id={project_id}",
        "auth": True,
        "required": False,  # GET is not mounted — POST-only; 404/405 is SKIP
        "note": "POST-only route; GET 404/405 is skip, not fail",
    },
    {
        "name": "factory.merge_check",
        "method": "POST",
        "path": "/v1/factory/merge-check",
        "auth": True,
        "required": False,
        "body_keys": ["project_id"],
        "note": "structural only unless --live-merge-check",
    },
    {
        "name": "factory.live_pr",
        "method": "POST",
        "path": "/v1/code-tasks",
        "auth": True,
        "required": False,
        "body_keys": ["project_id", "title", "instructions", "owner", "repo", "open_pr"],
        "note": (
            "optional live PR — fails closed without BOSS_QA_ALLOW_CANARIES=1; "
            "this slice does not open real PRs"
        ),
    },
]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    ok: bool
    required: bool = True
    skipped: bool = False
    detail: str = ""
    status_code: int | None = None
    duration_ms: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def redact_secrets(text: str, secrets: list[str] | None = None) -> str:
    if not text:
        return text
    redacted = text
    for secret in secrets or []:
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "***")
    if "X-Boss-Api-Key" in redacted:
        redacted = redacted.replace("X-Boss-Api-Key", "X-Boss-Api-Key(redacted)")
    return redacted


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def resolve_api_base(cli_value: str | None = None) -> str:
    return (
        (cli_value or "").strip()
        or os.environ.get("BOSS_API_BASE", "").strip()
        or os.environ.get("BOSS_API_URL", "").strip()
        or DEFAULT_API_URL
    )


def resolve_project_id(cli_value: str | None = None) -> str:
    return (
        (cli_value or "").strip()
        or os.environ.get("PROJECT_ID", "").strip()
        or os.environ.get("BOSS_PROJECT_ID", "").strip()
        or os.environ.get("BOSS_DEFAULT_PROJECT_ID", "").strip()
        or DEFAULT_PROJECT_ID
    )


def resolve_api_key(cli_value: str | None = None) -> str | None:
    key = (
        (cli_value or "").strip()
        or os.environ.get("BOSS_API_KEY", "").strip()
        or os.environ.get("NEXT_PUBLIC_BOSS_API_KEY", "").strip()
    )
    return key or None


def is_2xx(status_code: int | None) -> bool:
    return status_code is not None and 200 <= status_code < 300


def classify_proposals_queue(status_code: int | None) -> str:
    """GET /v1/software-factory/proposals/queue is not a list route.

    The live route is POST-only (queue a propose_pr job). GET therefore
    returns 405 (or 404 on older editions). That is a skip, not an optional
    FAIL that dirties the report.
    """
    if is_2xx(status_code):
        return "pass"
    if status_code in PROPOSALS_QUEUE_SKIP_STATUSES:
        return "skip"
    return "fail"


def fixture_create_allowed(wip_payload: Any) -> bool:
    """Only POST simulated=true when the live API advertises support.

    Older APIs ignore unknown body keys and would enqueue a real budgeted
    ``code.task``. Refusing to POST is the fail-closed choice.
    """
    return isinstance(wip_payload, dict) and wip_payload.get("fixture_supported") is True


def item_count(payload: Any) -> int | None:
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return None
    for key in ("items", "runs", "code_tasks", "proposals", "wip", "tasks"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def extract_run_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("run_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def code_task_create_body(
    project_id: str,
    *,
    owner: str,
    repo: str,
    base_branch: str = "main",
    run_tag: str | None = None,
) -> dict[str, Any]:
    tag = run_tag or uuid.uuid4().hex[:8]
    return {
        "project_id": project_id,
        "title": f"{COMMAND_PREFIX} structural probe {tag}",
        "instructions": (
            f"{COMMAND_PREFIX} dry structural create for project {project_id}. "
            "No production changes. Prefer a no-op README touch if work proceeds."
        ),
        "acceptance_criteria": ["Gate probe completes or is cancelled without side effects"],
        "test_commands": [],
        "owner": owner,
        "repo": repo,
        "base_branch": base_branch,
        "profile": "lean",
        "open_pr": False,
    }


def code_task_fixture_body(
    project_id: str,
    *,
    run_tag: str | None = None,
) -> dict[str, Any]:
    """No-spend simulated create. Completes without a live LLM."""
    tag = run_tag or uuid.uuid4().hex[:8]
    return {
        "project_id": project_id,
        "title": f"{COMMAND_PREFIX} simulated fixture {tag}",
        "instructions": (
            f"{COMMAND_PREFIX} simulated=true fixture for project {project_id}. "
            "Must not call a model, reserve budget, enqueue code.task, or open a PR."
        ),
        "acceptance_criteria": ["Fixture persists and is readable without spend"],
        "test_commands": [],
        "owner": "_fixture",
        "repo": "_fixture",
        "base_branch": "main",
        "profile": "lean",
        "open_pr": False,
        "simulated": True,
    }


def resolve_factory_repo() -> tuple[str | None, str | None, str]:
    owner = (
        os.environ.get("BOSS_FACTORY_OWNER", "").strip()
        or os.environ.get("GITHUB_OWNER", "").strip()
        or None
    )
    repo = (
        os.environ.get("BOSS_FACTORY_REPO", "").strip()
        or os.environ.get("GITHUB_REPO", "").strip()
        or None
    )
    base = (
        os.environ.get("BOSS_FACTORY_BASE_BRANCH", "").strip()
        or "main"
    )
    return owner, repo, base


def allow_create_from_env() -> bool:
    return os.environ.get("BOSS_FACTORY_ALLOW_CREATE", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    )


def allow_live_pr_from_env() -> bool:
    """Live PR attempts spend and open PRs. Fail closed unless canaries are explicit."""
    return os.environ.get("BOSS_QA_ALLOW_CANARIES", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    )


LIVE_PR_UNKNOWN_LABEL = "unknown / not proven"


def honesty_from_wip(wip_payload: Any) -> dict[str, Any]:
    """Read-only sealed-slice fields from GET /v1/factory/wip. Never invent a %."""
    if not isinstance(wip_payload, dict):
        return {
            "github_plane": None,
            "last_simulated_fixture": None,
            "live_pr_success": {
                "rate": None,
                "label": LIVE_PR_UNKNOWN_LABEL,
                "proven": False,
                "sample_size": 0,
            },
        }
    live = wip_payload.get("live_pr_success")
    if not isinstance(live, dict):
        live = {
            "rate": None,
            "label": LIVE_PR_UNKNOWN_LABEL,
            "proven": False,
            "sample_size": 0,
        }
    label = live.get("label") if isinstance(live.get("label"), str) else LIVE_PR_UNKNOWN_LABEL
    if label.strip().endswith("%") and "/" not in label:
        label = LIVE_PR_UNKNOWN_LABEL
    return {
        "github_plane": wip_payload.get("github_plane"),
        "last_simulated_fixture": wip_payload.get("last_simulated_fixture"),
        "live_pr_success": {
            "rate": live.get("rate"),
            "label": label or LIVE_PR_UNKNOWN_LABEL,
            "proven": live.get("proven") is True,
            "sample_size": int(live.get("sample_size") or 0),
        },
    }


def _ensure_engine_on_path(root: Path | None = None) -> None:
    engine = str((root or REPO_ROOT) / "engine")
    if engine not in sys.path:
        sys.path.insert(0, engine)


def _try_import_engine(module_name: str) -> Any | None:
    """Import a boss_engine module when PYTHONPATH allows; otherwise None."""
    try:
        _ensure_engine_on_path()
        return importlib.import_module(module_name)
    except Exception:
        return None


def _export_in_source(name: str, source: str) -> bool:
    return bool(
        re.search(rf"^def {re.escape(name)}\s*\(", source, re.M)
        or re.search(rf"^{re.escape(name)}\s*=", source, re.M)
    )


def inspect_quality_loop() -> dict[str, Any]:
    """Prove the factory quality-loop module exists and exports the public API.

    Prefers a real import; falls back to file-text inspection so dry-run
    stays offline and does not require engine on PYTHONPATH.
    """
    path = REPO_ROOT / QUALITY_LOOP_RELPATH
    exists = path.is_file()
    source = ""
    if exists:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            source = ""
    module = _try_import_engine("boss_engine.services.factory_quality_loop") if exists else None
    exports: dict[str, bool] = {}
    for name in QUALITY_LOOP_EXPORTS:
        if module is not None:
            exports[name] = callable(getattr(module, name, None))
        else:
            exports[name] = _export_in_source(name, source)
    return {
        "exists": exists,
        "imported": module is not None,
        "path": str(path),
        "exports": exports,
    }


def inspect_quality_loop_contract() -> dict[str, Any]:
    """Max 1 quality pass; never invent scores; do not edit frozen tests."""
    path = REPO_ROOT / QUALITY_LOOP_RELPATH
    source = ""
    if path.is_file():
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            source = ""
    module = _try_import_engine("boss_engine.services.factory_quality_loop")
    max_passes: int | None = None
    prompt_text = ""
    if module is not None:
        raw = getattr(module, "MAX_QUALITY_PASSES", None)
        if isinstance(raw, int):
            max_passes = raw
        prompt_fn = getattr(module, "quality_improve_prompt", None)
        if callable(prompt_fn):
            try:
                prompt_text = str(prompt_fn({}, title="factory-e2e-gate"))
            except Exception:
                prompt_text = ""
    if max_passes is None:
        match = re.search(r"^MAX_QUALITY_PASSES\s*=\s*(\d+)\s*$", source, re.M)
        if match:
            max_passes = int(match.group(1))
    haystack = f"{source}\n{prompt_text}"
    return {
        "max_quality_passes": max_passes,
        "never_invent_scores": bool(re.search(r"(never invent|do not invent)", haystack, re.I)),
        "do_not_edit_frozen_tests": bool(
            re.search(r"frozen test", haystack, re.I)
            and re.search(r"(do not edit|must not edit)", haystack, re.I)
        ),
    }


def inspect_scorecard_v11() -> dict[str, Any]:
    """Scorecard v1.1 must include dedicated ui and ux dimensions."""
    path = REPO_ROOT / BENCHMARK_APPS_RELPATH
    module = _try_import_engine("boss_engine.services.benchmark_apps")
    version: str | None = None
    dim_ids: list[str] = []
    imported = False
    if module is not None:
        imported = True
        raw_version = getattr(module, "SCORECARD_VERSION", None)
        version = raw_version if isinstance(raw_version, str) else None
        for item in getattr(module, "SCORING_DIMENSIONS", ()) or ():
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                dim_ids.append(item["id"])
    elif path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        match = re.search(r'^SCORECARD_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
        version = match.group(1) if match else None
        block = re.search(r"SCORING_DIMENSIONS\s*=\s*\(", text)
        blob = text[block.end() : block.end() + 4000] if block else text
        dim_ids = re.findall(r'["\']id["\']\s*:\s*["\']([a-z0-9_]+)["\']', blob)
    return {
        "version": version,
        "dimension_ids": dim_ids,
        "has_ui": "ui" in dim_ids,
        "has_ux": "ux" in dim_ids,
        "imported": imported,
        "path": str(path),
    }


def factory_catalog_paths() -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for endpoint in STRUCTURAL_ENDPOINTS:
        raw = str(endpoint.get("path") or "").split("?", 1)[0]
        if raw and raw not in seen:
            seen.add(raw)
            paths.append(raw)
    return paths


def quality_surface_checks() -> list[CheckResult]:
    """No-network proofs of the factory quality loop + remaining endpoints."""
    checks: list[CheckResult] = []

    loop = inspect_quality_loop()
    missing = [name for name, present in loop["exports"].items() if not present]
    checks.append(
        CheckResult(
            name="structure.quality_loop_module",
            ok=bool(loop["exists"]) and not missing,
            detail=(
                f"exports={sorted(loop['exports'])} imported={loop['imported']}"
                if loop["exists"] and not missing
                else f"missing module or exports {missing} exists={loop['exists']}"
            ),
            evidence={
                "path": loop["path"],
                "exists": loop["exists"],
                "imported": loop["imported"],
                "exports": loop["exports"],
            },
        )
    )

    card = inspect_scorecard_v11()
    checks.append(
        CheckResult(
            name="structure.scorecard_v11_ui_ux",
            ok=card["version"] == "1.1" and card["has_ui"] and card["has_ux"],
            detail=f"version={card['version']} dims={card['dimension_ids']}",
            evidence={
                "version": card["version"],
                "dimension_ids": card["dimension_ids"],
                "imported": card["imported"],
            },
        )
    )

    paths = factory_catalog_paths()
    checks.append(
        CheckResult(
            name="structure.factory_endpoints_remain",
            ok="/v1/factory/wip" in paths and "/v1/code-tasks" in paths,
            detail=f"paths={paths}",
            evidence={"paths": paths},
        )
    )

    contract = inspect_quality_loop_contract()
    checks.append(
        CheckResult(
            name="structure.quality_loop_contract",
            ok=(
                contract["max_quality_passes"] == 1
                and contract["never_invent_scores"] is True
                and contract["do_not_edit_frozen_tests"] is True
            ),
            detail=(
                f"MAX_QUALITY_PASSES={contract['max_quality_passes']} "
                "never invent scores; do not edit frozen tests"
            ),
            evidence=contract,
        )
    )

    create = next(e for e in STRUCTURAL_ENDPOINTS if e["name"] == "code_tasks.create")
    checks.append(
        CheckResult(
            name="structure.no_budgeted_code_task",
            ok=create.get("required") is False and create.get("method") == "POST",
            detail=(
                "dry-run and default live never POST a budgeted code.task; "
                "create stays opt-in"
            ),
            evidence={
                "create_required": bool(create.get("required")),
                "create_method": create.get("method"),
            },
        )
    )
    return checks


def validate_dry_run_structure(
    *,
    api_base: str = DEFAULT_API_URL,
    project_id: str = DEFAULT_PROJECT_ID,
) -> list[CheckResult]:
    """Structure-only validation of factory endpoint contracts — no network."""
    checks: list[CheckResult] = []

    names = [e["name"] for e in STRUCTURAL_ENDPOINTS]
    required_present = {"health", "factory.wip", "code_tasks.list", "code_tasks.create"}
    missing = required_present - set(names)
    checks.append(
        CheckResult(
            name="structure.endpoint_catalog",
            ok=not missing,
            detail=f"catalog={names}" if not missing else f"missing {sorted(missing)}",
            evidence={"names": names},
        )
    )

    for endpoint in STRUCTURAL_ENDPOINTS:
        path = endpoint["path"].format(project_id=project_id)
        url = join_url(api_base, path)
        method = endpoint["method"]
        ok = method in ("GET", "POST", "PUT", "PATCH", "DELETE") and url.startswith(
            ("http://", "https://")
        )
        checks.append(
            CheckResult(
                name=f"structure.endpoint.{endpoint['name']}",
                ok=ok,
                required=bool(endpoint.get("required", True)),
                detail=f"{method} {url}",
                evidence={
                    "auth": endpoint.get("auth"),
                    "body_keys": endpoint.get("body_keys"),
                },
            )
        )

    # create body shape
    body = code_task_create_body(
        project_id, owner="example-org", repo="example-repo", run_tag="dry"
    )
    need = {"project_id", "title", "instructions", "owner", "repo", "base_branch"}
    missing_keys = need - set(body)
    checks.append(
        CheckResult(
            name="structure.code_task_body",
            ok=not missing_keys and COMMAND_PREFIX in body["title"],
            detail="ok" if not missing_keys else f"missing {sorted(missing_keys)}",
            evidence={"keys": sorted(body.keys())},
        )
    )

    fixture = code_task_fixture_body(project_id, run_tag="dry")
    fixture_need = need | {"simulated"}
    fixture_missing = fixture_need - set(fixture)
    checks.append(
        CheckResult(
            name="structure.code_task_fixture_body",
            ok=(
                not fixture_missing
                and fixture.get("simulated") is True
                and fixture.get("open_pr") is False
            ),
            detail="ok" if not fixture_missing else f"missing {sorted(fixture_missing)}",
            evidence={"keys": sorted(fixture.keys()), "simulated": fixture.get("simulated")},
        )
    )

    checks.append(
        CheckResult(
            name="structure.proposals_queue_skip",
            ok=(
                classify_proposals_queue(405) == "skip"
                and classify_proposals_queue(404) == "skip"
                and classify_proposals_queue(200) == "pass"
                and classify_proposals_queue(500) == "fail"
            ),
            detail="GET 404/405 on proposals/queue is SKIP (POST-only route)",
        )
    )

    # helpers
    checks.append(
        CheckResult(
            name="structure.helpers",
            ok=(
                is_2xx(201)
                and not is_2xx(500)
                and extract_run_id({"run_id": "r1", "status": "queued"}) == "r1"
            ),
            detail="is_2xx + extract_run_id",
        )
    )

    empty_honesty = honesty_from_wip({})
    fake_pct = honesty_from_wip({"live_pr_success": {"rate": 0.87, "label": "87%"}})
    checks.append(
        CheckResult(
            name="structure.live_pr_honesty",
            ok=(
                empty_honesty["live_pr_success"]["label"] == LIVE_PR_UNKNOWN_LABEL
                and empty_honesty["live_pr_success"]["rate"] is None
                and fake_pct["live_pr_success"]["label"] == LIVE_PR_UNKNOWN_LABEL
            ),
            detail=(
                "empty live PR rate is unknown / not proven; invented % rejected; "
                "BOSS_QA_ALLOW_CANARIES unset in dry-run"
            ),
        )
    )

    checks.extend(quality_surface_checks())

    # document live env requirements in evidence for operators reading dry-run report
    checks.append(
        CheckResult(
            name="structure.live_env_docs",
            ok=True,
            required=False,
            detail=(
                "full live create needs BOSS_API_BASE, BOSS_API_KEY, PROJECT_ID, "
                "BOSS_FACTORY_OWNER, BOSS_FACTORY_REPO, BOSS_FACTORY_ALLOW_CREATE=1"
            ),
            evidence={
                "required_read_only": [
                    "BOSS_API_BASE|BOSS_API_URL",
                    "BOSS_API_KEY",
                    "PROJECT_ID|BOSS_PROJECT_ID",
                ],
                "required_create": [
                    "BOSS_FACTORY_OWNER|GITHUB_OWNER",
                    "BOSS_FACTORY_REPO|GITHUB_REPO",
                    "BOSS_FACTORY_ALLOW_CREATE=1",
                    "--create-code-task",
                ],
                "optional_fixture": [
                    "--create-fixture (no spend; needs fixture_supported on factory/wip)",
                ],
                "optional_live_pr": [
                    "--live-pr fails closed without BOSS_QA_ALLOW_CANARIES=1 (no POST)",
                ],
            },
        )
    )

    return checks


def summarize_report(checks: list[CheckResult]) -> dict[str, Any]:
    required = [c for c in checks if c.required]
    failed_required = [c for c in required if not c.ok and not c.skipped]
    optional = [c for c in checks if not c.required]
    skipped = [c for c in checks if c.skipped]
    failed_optional = [c for c in optional if not c.ok and not c.skipped]
    return {
        "total": len(checks),
        "required_total": len(required),
        "required_passed": len(required) - len(failed_required),
        "required_failed": len(failed_required),
        "optional_total": len(optional),
        "optional_passed": len(optional) - len(failed_optional) - sum(
            1 for c in optional if c.skipped
        ),
        "optional_failed": len(failed_optional),
        "skipped_total": len(skipped),
        "skipped_names": [c.name for c in skipped],
        "failed_required_names": [c.name for c in failed_required],
        "failed_optional_names": [c.name for c in failed_optional],
        "passed": len(failed_required) == 0,
    }


def build_report(
    *,
    api_url: str,
    project_id: str,
    checks: list[CheckResult],
    started_at: str,
    finished_at: str,
    duration_ms: int,
    mode: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "gate": GATE_NAME,
        "version": GATE_VERSION,
        "mode": mode,
        "api_url": api_url,
        "project_id": project_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "summary": summarize_report(checks),
        "checks": [c.to_dict() for c in checks],
        "meta": meta or {},
        "env_for_full_live": {
            "required": [
                "BOSS_API_BASE or BOSS_API_URL",
                "BOSS_API_KEY",
                "PROJECT_ID or BOSS_PROJECT_ID",
            ],
            "optional_create": [
                "BOSS_FACTORY_OWNER or GITHUB_OWNER",
                "BOSS_FACTORY_REPO or GITHUB_REPO",
                "BOSS_FACTORY_BASE_BRANCH (default main)",
                "BOSS_FACTORY_ALLOW_CREATE=1",
                "CLI flag --create-code-task",
            ],
            "optional_fixture": [
                "CLI flag --create-fixture (no spend; requires fixture_supported on factory/wip)",
            ],
            "optional_live_pr": [
                "CLI flag --live-pr fails closed without BOSS_QA_ALLOW_CANARIES=1 (no POST)",
            ],
        },
    }


def gate_exit_code(report: dict[str, Any]) -> int:
    summary = report.get("summary") or {}
    return 0 if summary.get("passed") else 1


def default_output_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for base in (Path("artifacts") / "qa", Path("/tmp") / "boss-qa" / "artifacts", Path("/tmp")):
        try:
            base.mkdir(parents=True, exist_ok=True)
            return base / f"factory-e2e-gate-{stamp}.json"
        except OSError:
            continue
    return Path(f"/tmp/factory-e2e-gate-{stamp}.json")


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class BossApiClient:
    def __init__(
        self,
        api_url: str,
        api_key: str | None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        secrets_to_redact: list[str] | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.secrets_to_redact = [s for s in (secrets_to_redact or []) if s]

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        auth: bool = True,
        timeout_seconds: float | None = None,
    ) -> tuple[int, Any, int]:
        url = join_url(self.api_url, path)
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        if auth and self.api_key:
            headers["X-Boss-Api-Key"] = self.api_key
        request = Request(url, data=encoded, headers=headers, method=method)
        started = time.monotonic()
        try:
            with urlopen(request, timeout=timeout_seconds or self.timeout_seconds) as response:  # noqa: S310
                raw = response.read().decode("utf-8", errors="replace")
                status = int(response.status)
        except HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            status = int(error.code)
        except URLError as error:
            duration_ms = int((time.monotonic() - started) * 1000)
            reason = redact_secrets(str(error.reason), self.secrets_to_redact)
            raise RuntimeError(f"BOSS API {method} {path} unreachable: {reason}") from error
        duration_ms = int((time.monotonic() - started) * 1000)
        raw = redact_secrets(raw, self.secrets_to_redact)
        if not raw.strip():
            return status, None, duration_ms
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return status, raw[:1000], duration_ms
        return status, payload, duration_ms


# ---------------------------------------------------------------------------
# Live gate
# ---------------------------------------------------------------------------


def run_gate(
    client: BossApiClient,
    *,
    project_id: str,
    create_code_task: bool = False,
    create_fixture: bool = False,
    live_pr: bool = False,
    owner: str | None = None,
    repo: str | None = None,
    base_branch: str = "main",
) -> list[CheckResult]:
    checks: list[CheckResult] = []

    def record(result: CheckResult) -> None:
        checks.append(result)

    # 1. health
    try:
        status, payload, duration_ms = client.request("GET", "/health", auth=False)
        ok = is_2xx(status)
        detail = f"status={status}"
        if isinstance(payload, dict):
            detail = f"status={status} health={payload.get('status')!r}"
            if payload.get("database_connected") is False:
                ok = False
                detail += " database_connected=false"
        record(
            CheckResult(
                name="health",
                ok=ok,
                detail=detail,
                status_code=status,
                duration_ms=duration_ms,
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="health", ok=False, detail=str(error)))

    # 2. factory WIP
    wip_payload: Any = None
    try:
        path = f"/v1/factory/wip?{urlencode({'project_id': project_id, 'wip_limit': 5})}"
        status, payload, duration_ms = client.request("GET", path, auth=True)
        ok = is_2xx(status)
        if ok:
            wip_payload = payload
        count = item_count(payload)
        record(
            CheckResult(
                name="factory.wip",
                ok=ok,
                detail=f"status={status} count={count}" if ok else f"factory/wip expected 2xx, got {status}",
                status_code=status,
                duration_ms=duration_ms,
                evidence={
                    "count": count,
                    "fixture_supported": (
                        payload.get("fixture_supported") if isinstance(payload, dict) else None
                    ),
                },
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="factory.wip", ok=False, detail=str(error)))

    honesty = honesty_from_wip(wip_payload)
    live = honesty["live_pr_success"]
    last_sim = honesty["last_simulated_fixture"]
    last_id = None
    if isinstance(last_sim, dict):
        last_id = last_sim.get("run_id") or last_sim.get("id")
    record(
        CheckResult(
            name="factory.sealed_slice_honesty",
            ok=True,
            required=False,
            detail=(
                f"github_plane={honesty.get('github_plane')!r} "
                f"last_simulated={last_id or 'none'} "
                f"live_pr={live.get('label')}"
            ),
            evidence=honesty,
        )
    )

    # 3. code-tasks list
    try:
        path = f"/v1/code-tasks?{urlencode({'project_id': project_id, 'limit': 20})}"
        status, payload, duration_ms = client.request("GET", path, auth=True)
        ok = is_2xx(status)
        count = item_count(payload)
        record(
            CheckResult(
                name="code_tasks.list",
                ok=ok,
                detail=f"status={status} count={count}" if ok else f"code-tasks list expected 2xx, got {status}",
                status_code=status,
                duration_ms=duration_ms,
                evidence={"count": count},
            )
        )
    except RuntimeError as error:
        record(CheckResult(name="code_tasks.list", ok=False, detail=str(error)))

    # 4a. optional no-spend fixture (never POSTs unless live API advertises support)
    if create_fixture:
        if not fixture_create_allowed(wip_payload):
            record(
                CheckResult(
                    name="code_tasks.create_fixture",
                    ok=True,
                    required=False,
                    skipped=True,
                    detail=(
                        "SKIP: live API did not advertise fixture_supported on factory/wip. "
                        "Refusing to POST simulated=true — older APIs ignore unknown keys "
                        "and would enqueue a real budgeted code.task. "
                        "No no-spend worker path exists (no deterministic_brief / dry-run)."
                    ),
                    evidence={"why": "no_spend_path_requires_simulated_flag_on_live_api"},
                )
            )
        else:
            try:
                body = code_task_fixture_body(project_id)
                status, payload, duration_ms = client.request(
                    "POST",
                    "/v1/code-tasks",
                    body=body,
                    auth=True,
                )
                run_id = extract_run_id(payload) if is_2xx(status) else None
                simulated_flag = (
                    isinstance(payload, dict) and payload.get("simulated") is True
                )
                ok = is_2xx(status) and simulated_flag and bool(run_id)
                record(
                    CheckResult(
                        name="code_tasks.create_fixture",
                        ok=ok,
                        required=True,
                        detail=(
                            f"status={status} run_id={run_id} simulated=true"
                            if ok
                            else (
                                f"fixture create expected 2xx + simulated=true, got {status}"
                            )
                        ),
                        status_code=status,
                        duration_ms=duration_ms,
                        evidence={"run_id": run_id, "simulated": simulated_flag},
                    )
                )
                if ok and run_id:
                    try:
                        ev_status, ev_payload, ev_ms = client.request(
                            "GET",
                            f"/v1/code-tasks/{run_id}",
                            auth=True,
                        )
                        spec = ev_payload.get("spec") if isinstance(ev_payload, dict) else {}
                        usage = ev_payload.get("usage") if isinstance(ev_payload, dict) else {}
                        ev_ok = (
                            is_2xx(ev_status)
                            and isinstance(ev_payload, dict)
                            and ev_payload.get("status") == "simulated"
                            and spec.get("simulated") is True
                            and int((usage or {}).get("cost_usd_micros") or 0) == 0
                        )
                        record(
                            CheckResult(
                                name="code_tasks.fixture_evidence",
                                ok=ev_ok,
                                required=True,
                                detail=(
                                    f"status={ev_status} run_status={ev_payload.get('status')!r} "
                                    f"cost={((usage or {}).get('cost_usd_micros') if isinstance(usage, dict) else None)}"
                                    if isinstance(ev_payload, dict)
                                    else f"status={ev_status}"
                                ),
                                status_code=ev_status,
                                duration_ms=ev_ms,
                                evidence={
                                    "run_id": run_id,
                                    "run_status": (
                                        ev_payload.get("status")
                                        if isinstance(ev_payload, dict)
                                        else None
                                    ),
                                    "cost_usd_micros": (
                                        (usage or {}).get("cost_usd_micros")
                                        if isinstance(usage, dict)
                                        else None
                                    ),
                                },
                            )
                        )
                    except RuntimeError as error:
                        record(
                            CheckResult(
                                name="code_tasks.fixture_evidence",
                                ok=False,
                                required=True,
                                detail=str(error),
                            )
                        )
            except RuntimeError as error:
                record(
                    CheckResult(
                        name="code_tasks.create_fixture",
                        ok=False,
                        required=True,
                        detail=str(error),
                    )
                )
    else:
        record(
            CheckResult(
                name="code_tasks.create_fixture",
                ok=True,
                required=False,
                skipped=True,
                detail="skipped (pass --create-fixture for no-spend simulated create)",
            )
        )

    # 4b. optional real create
    if create_code_task:
        if not owner or not repo:
            record(
                CheckResult(
                    name="code_tasks.create",
                    ok=False,
                    required=True,
                    detail=(
                        "create requested but owner/repo missing "
                        "(set BOSS_FACTORY_OWNER + BOSS_FACTORY_REPO)"
                    ),
                )
            )
        elif not allow_create_from_env():
            record(
                CheckResult(
                    name="code_tasks.create",
                    ok=False,
                    required=True,
                    detail=(
                        "create requested but BOSS_FACTORY_ALLOW_CREATE is not set to 1 "
                        "(refusing to queue budgeted work without explicit allow)"
                    ),
                )
            )
        else:
            try:
                body = code_task_create_body(
                    project_id,
                    owner=owner,
                    repo=repo,
                    base_branch=base_branch,
                )
                status, payload, duration_ms = client.request(
                    "POST",
                    "/v1/code-tasks",
                    body=body,
                    auth=True,
                )
                ok = is_2xx(status)
                run_id = extract_run_id(payload) if ok else None
                record(
                    CheckResult(
                        name="code_tasks.create",
                        ok=ok,
                        required=True,
                        detail=(
                            f"status={status} run_id={run_id}"
                            if ok
                            else f"code-tasks create expected 2xx, got {status}"
                        ),
                        status_code=status,
                        duration_ms=duration_ms,
                        evidence={"run_id": run_id, "owner": owner, "repo": repo},
                    )
                )
            except RuntimeError as error:
                record(
                    CheckResult(
                        name="code_tasks.create",
                        ok=False,
                        required=True,
                        detail=str(error),
                    )
                )
    else:
        record(
            CheckResult(
                name="code_tasks.create",
                ok=True,
                required=False,
                detail="skipped (pass --create-code-task + BOSS_FACTORY_ALLOW_CREATE=1 for live create)",
            )
        )

    # 5. proposals queue — GET is not mounted (POST-only). 404/405 = SKIP.
    try:
        path = (
            f"/v1/software-factory/proposals/queue?"
            f"{urlencode({'project_id': project_id})}"
        )
        status, payload, duration_ms = client.request("GET", path, auth=True)
        verdict = classify_proposals_queue(status)
        if verdict == "skip":
            record(
                CheckResult(
                    name="factory.proposals_queue",
                    ok=True,
                    required=False,
                    skipped=True,
                    detail=(
                        f"{status} — GET not supported on proposals/queue "
                        "(POST-only route; skip, not fail)"
                    ),
                    status_code=status,
                    duration_ms=duration_ms,
                    evidence={"verdict": "skip", "reason": "method_not_allowed_or_unmounted"},
                )
            )
        else:
            count = item_count(payload)
            record(
                CheckResult(
                    name="factory.proposals_queue",
                    ok=verdict == "pass",
                    required=False,
                    detail=f"status={status} count={count}",
                    status_code=status,
                    duration_ms=duration_ms,
                    evidence={"count": count, "verdict": verdict},
                )
            )
    except RuntimeError as error:
        record(
            CheckResult(
                name="factory.proposals_queue",
                ok=False,
                required=False,
                detail=str(error),
            )
        )

    # 6. optional live PR — fail closed without BOSS_QA_ALLOW_CANARIES. Never POST.
    if live_pr:
        if not allow_live_pr_from_env():
            record(
                CheckResult(
                    name="factory.live_pr",
                    ok=False,
                    required=False,
                    detail=(
                        "FAIL CLOSED: live PR requires BOSS_QA_ALLOW_CANARIES=1. "
                        f"live PR success rate: {LIVE_PR_UNKNOWN_LABEL}. No POST."
                    ),
                    evidence={
                        "why": "canaries_not_allowed",
                        "posts": 0,
                        "live_pr_success": honesty["live_pr_success"],
                    },
                )
            )
        else:
            record(
                CheckResult(
                    name="factory.live_pr",
                    ok=False,
                    required=False,
                    detail=(
                        "FAIL CLOSED: this no-spend slice does not open real PRs even "
                        f"with canaries allowed. live PR success rate: {LIVE_PR_UNKNOWN_LABEL}."
                    ),
                    evidence={
                        "why": "live_pr_not_implemented_no_spend",
                        "posts": 0,
                        "live_pr_success": honesty["live_pr_success"],
                    },
                )
            )
    else:
        record(
            CheckResult(
                name="factory.live_pr",
                ok=True,
                required=False,
                skipped=True,
                detail=(
                    "skipped (pass --live-pr to request the optional live PR check; "
                    "fails closed without BOSS_QA_ALLOW_CANARIES=1)"
                ),
            )
        )

    # Structure + contract proofs also run in live read-only (no extra POSTs).
    for extra in quality_surface_checks():
        record(extra)

    return checks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Factory E2E gate: factory WIP + code-tasks (+ optional create).",
    )
    parser.add_argument(
        "--api-base",
        "--api-url",
        dest="api_base",
        default=None,
        help="BOSS API base (env: BOSS_API_BASE or BOSS_API_URL).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="BOSS API key (env: BOSS_API_KEY).",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Project id (env: PROJECT_ID or BOSS_PROJECT_ID).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Structure check only — no network. Also used when API key is missing.",
    )
    parser.add_argument(
        "--create-code-task",
        action="store_true",
        help=(
            "Optionally POST /v1/code-tasks (requires BOSS_FACTORY_ALLOW_CREATE=1 "
            "and owner/repo env). Spends budget."
        ),
    )
    parser.add_argument(
        "--create-fixture",
        action="store_true",
        help=(
            "POST /v1/code-tasks with simulated=true (no spend) when the live API "
            "advertises fixture_supported. Not a sealed factory path."
        ),
    )
    parser.add_argument(
        "--live-pr",
        action="store_true",
        dest="live_pr",
        help=(
            "Optional live PR proof. Fails closed without BOSS_QA_ALLOW_CANARIES=1 "
            "and does not POST. Required checks stay no-spend."
        ),
    )
    parser.add_argument(
        "--owner",
        default=None,
        help="GitHub owner override for create (env: BOSS_FACTORY_OWNER).",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="GitHub repo override for create (env: BOSS_FACTORY_REPO).",
    )
    parser.add_argument(
        "--base-branch",
        default=None,
        help="Base branch for create (env: BOSS_FACTORY_BASE_BRANCH, default main).",
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("BOSS_FACTORY_GATE_OUTPUT"),
        help="JSON report path.",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def check_flag(check: CheckResult) -> str:
    if check.skipped:
        return "SKIP"
    return "PASS" if check.ok else "FAIL"


def _print_checks(checks: list[CheckResult], quiet: bool) -> None:
    if quiet:
        return
    for check in checks:
        flag = check_flag(check)
        req = "required" if check.required else "optional"
        code = f" [{check.status_code}]" if check.status_code is not None else ""
        print(f"  {flag}  {check.name} ({req}){code}: {check.detail}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    api_base = resolve_api_base(args.api_base)
    project_id = resolve_project_id(args.project_id)
    api_key = resolve_api_key(args.api_key)
    env_owner, env_repo, env_base = resolve_factory_repo()
    owner = (args.owner or env_owner or "").strip() or None
    repo = (args.repo or env_repo or "").strip() or None
    base_branch = (args.base_branch or env_base or "main").strip() or "main"

    # Auto dry-run when no key and not explicitly live-attempting
    force_dry = args.dry_run or not api_key

    started = datetime.now(timezone.utc)
    started_mono = time.monotonic()

    if args.create_code_task and args.create_fixture:
        print(
            "error: --create-code-task and --create-fixture are mutually exclusive. "
            "Fixture is the no-spend path; create-code-task spends budget.",
            file=sys.stderr,
        )
        return 2

    if force_dry:
        if (args.create_code_task or args.create_fixture) and not args.dry_run and not api_key:
            which = "--create-fixture" if args.create_fixture else "--create-code-task"
            print(
                f"error: {which} requires BOSS_API_KEY. "
                "Use --dry-run for structure-only checks.",
                file=sys.stderr,
            )
            return 2
        checks = validate_dry_run_structure(api_base=api_base, project_id=project_id)
        mode = "dry-run"
        if not api_key and not args.dry_run:
            # Implicit dry-run: make it obvious
            print(
                f"[{GATE_NAME}] no BOSS_API_KEY — running structural dry-run "
                "(pass key for live read-only probes).",
                file=sys.stderr,
            )
    else:
        client = BossApiClient(
            api_base,
            api_key,
            timeout_seconds=args.timeout_seconds,
            secrets_to_redact=[api_key] if api_key else [],
        )
        try:
            checks = run_gate(
                client,
                project_id=project_id,
                create_code_task=bool(args.create_code_task),
                create_fixture=bool(args.create_fixture),
                live_pr=bool(args.live_pr),
                owner=owner,
                repo=repo,
                base_branch=base_branch,
            )
        except Exception as error:  # noqa: BLE001
            checks = [
                CheckResult(
                    name="gate.crash",
                    ok=False,
                    detail=redact_secrets(str(error), [api_key] if api_key else []),
                )
            ]
        if args.create_fixture:
            mode = "live+fixture"
        elif args.create_code_task:
            mode = "live+create"
        else:
            mode = "live+readonly"

    finished = datetime.now(timezone.utc)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    report = build_report(
        api_url=api_base,
        project_id=project_id,
        checks=checks,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_ms=duration_ms,
        mode=mode,
        meta={
            "create_requested": bool(args.create_code_task),
            "fixture_requested": bool(args.create_fixture),
            "live_pr_requested": bool(args.live_pr),
            "owner_set": bool(owner),
            "repo_set": bool(repo),
            "allow_create_env": allow_create_from_env(),
            "allow_live_pr_env": allow_live_pr_from_env(),
        },
    )
    exit_code = gate_exit_code(report)

    out_path = Path(args.output) if args.output else default_output_path()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"warning: could not write report: {error}", file=sys.stderr)
        out_path = Path("(unwritten)")

    summary = report["summary"]
    overall = "PASS" if summary["passed"] else "FAIL"
    print(
        f"[{GATE_NAME}] mode={mode} overall={overall} "
        f"required={summary['required_passed']}/{summary['required_total']} "
        f"duration_ms={duration_ms}"
    )
    _print_checks(checks, args.quiet)
    print(f"report: {out_path}")
    if not summary["passed"]:
        print(f"failed required: {summary['failed_required_names']}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
