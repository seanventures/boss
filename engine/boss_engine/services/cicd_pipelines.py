"""CI/CD multi-vendor catalog, dry-run validate, and pipeline-run provenance ledger.

This is the BOSS-side product surface for:

* Catalog of supported CI vendors and config templates
* Deterministic config generation (via CIConfigGenerator)
* Structure-level YAML validation (dry-run, no cloud calls)
* Provenance records for pipeline runs observed or recorded by operators

It does **not** call live GitHub/GitLab/Jenkins/CircleCI/Buildkite APIs. Live
adapter polling remains behind ``pipeline_adapter`` + credentials. Provider
deployment remains ChangeSet-gated in the Resource Plane.
"""
from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import yaml

from .ci_config_generator import (
    SUPPORTED_TARGETS,
    TARGET_PATHS,
    CIConfigGenerator,
)


def _make_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20]}"


# Market-facing vendor catalog (templates + live adapter capability hints).
# ``generates_config`` means BOSS can emit a checked-in config file.
# ``live_adapter`` means a PipelineAdapter kind exists for poll/verdict (may
# still require credentials to be configured).
VENDOR_CATALOG: list[dict[str, Any]] = [
    {
        "id": "github_actions",
        "name": "GitHub Actions",
        "config_path": TARGET_PATHS["github_actions"],
        "generates_config": True,
        "live_adapter": True,
        "adapter_kind": "github_actions",
        "description": (
            "Workflow YAML under .github/workflows. Deploy job is manual "
            "workflow_dispatch; provider actuation stays ChangeSet-gated."
        ),
        "languages": ["python", "javascript", "typescript", "go"],
    },
    {
        "id": "gitlab_ci",
        "name": "GitLab CI",
        "config_path": TARGET_PATHS["gitlab_ci"],
        "generates_config": True,
        "live_adapter": True,
        "adapter_kind": "gitlab",
        "description": (
            "Root .gitlab-ci.yml with test/build/manual deploy stages. "
            "Live adapter polls GitLab pipelines when token is configured."
        ),
        "languages": ["python", "javascript", "typescript", "go"],
    },
    {
        "id": "jenkins",
        "name": "Jenkins",
        "config_path": TARGET_PATHS["jenkins"],
        "generates_config": True,
        "live_adapter": True,
        "adapter_kind": "jenkins",
        "description": (
            "Declarative Jenkinsfile. Live adapter talks to Jenkins API when "
            "base URL and credentials are configured."
        ),
        "languages": ["python", "javascript", "typescript", "go"],
    },
    {
        "id": "circleci",
        "name": "CircleCI",
        "config_path": TARGET_PATHS["circleci"],
        "generates_config": True,
        "live_adapter": False,
        "adapter_kind": None,
        "description": (
            "CircleCI 2.1 config with approval-gated deploy. Template only — "
            "no live cloud adapter without an explicit future integration."
        ),
        "languages": ["python", "javascript", "typescript", "go"],
    },
    {
        "id": "buildkite",
        "name": "Buildkite",
        "config_path": TARGET_PATHS["buildkite"],
        "generates_config": True,
        "live_adapter": False,
        "adapter_kind": None,
        "description": (
            "Buildkite pipeline YAML with block step for deploy handoff. "
            "Template only — no live cloud adapter without an explicit future integration."
        ),
        "languages": ["python", "javascript", "typescript", "go"],
    },
]


_RUN_LOCK = threading.RLock()
# In-process provenance ledger: project_id -> list[run dict] (newest first).
_RUNS: dict[str, list[dict[str, Any]]] = {}
# dedupe_key -> run_id for idempotent record_run
_DEDUPE: dict[str, str] = {}

_VALID_RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "passed",
        "failed",
        "cancelled",
        "skipped",
        "unknown",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def catalog() -> dict[str, Any]:
    """Return the multi-vendor CI/CD catalog (no secrets, no live calls)."""
    return {
        "vendors": deepcopy(VENDOR_CATALOG),
        "count": len(VENDOR_CATALOG),
        "supported_generate_targets": list(SUPPORTED_TARGETS),
        "notes": [
            "Generate is dry-run only — no PR, no provider write.",
            "Validate checks YAML structure only; it does not call vendor APIs.",
            "Pipeline run records are a BOSS-side provenance ledger.",
            "Live poll/verdict requires pipeline_adapter + configured credentials.",
            "Provider deployment remains ChangeSet-gated in the Resource Plane.",
        ],
    }


def generate(
    *,
    vendor: str,
    language: str,
    framework: str | None = None,
) -> dict[str, Any]:
    """Generate a CI config for vendor+language (dry-run)."""
    generator = CIConfigGenerator()
    # Map vendor catalog ids / aliases onto generator targets.
    target = (vendor or "").strip().lower().replace("-", "_")
    result = generator.generate(
        language=language,
        framework=framework,
        target=target,
    )
    result["vendor"] = result["target"]
    return result


def validate_yaml_structure(
    yaml_text: str,
    *,
    vendor: str | None = None,
) -> dict[str, Any]:
    """Dry-run structural validation of CI YAML (or Jenkinsfile text).

    Checks:
    * non-empty input
    * YAML parse (or Groovy-ish Jenkinsfile heuristic)
    * optional vendor-specific required keys
    Does not call any external API.
    """
    text = yaml_text if isinstance(yaml_text, str) else ""
    errors: list[str] = []
    warnings: list[str] = []
    parsed: Any = None
    normalized_vendor = (vendor or "").strip().lower().replace("-", "_") or None
    if normalized_vendor in {"gha", "github", "actions"}:
        normalized_vendor = "github_actions"
    if normalized_vendor in {"gitlab", "gitlabci"}:
        normalized_vendor = "gitlab_ci"
    if normalized_vendor in {"circle", "circle_ci"}:
        normalized_vendor = "circleci"
    if normalized_vendor in {"bk"}:
        normalized_vendor = "buildkite"

    if not text.strip():
        errors.append("yaml text is empty")
        return {
            "valid": False,
            "vendor": normalized_vendor,
            "errors": errors,
            "warnings": warnings,
            "summary": {"keys": [], "kind": None},
        }

    # Jenkinsfile is Groovy, not YAML.
    if normalized_vendor == "jenkins" or (
        normalized_vendor is None
        and "pipeline {" in text
        and "stages {" in text
    ):
        if "pipeline" not in text:
            errors.append("Jenkinsfile must contain a pipeline { ... } block")
        if "stages" not in text and "stage(" not in text:
            warnings.append("No stages detected in Jenkinsfile")
        return {
            "valid": len(errors) == 0,
            "vendor": normalized_vendor or "jenkins",
            "errors": errors,
            "warnings": warnings,
            "summary": {
                "keys": ["pipeline"],
                "kind": "jenkinsfile",
                "has_stages": "stage" in text.lower(),
            },
        }

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        errors.append(f"YAML parse error: {exc}")
        return {
            "valid": False,
            "vendor": normalized_vendor,
            "errors": errors,
            "warnings": warnings,
            "summary": {"keys": [], "kind": None},
        }

    if parsed is None:
        errors.append("YAML parsed to null / empty document")
    elif not isinstance(parsed, (dict, list)):
        errors.append(
            f"Top-level YAML must be a mapping or sequence, got {type(parsed).__name__}"
        )

    keys: list[str] = []
    kind: str | None = None
    if isinstance(parsed, dict):
        keys = sorted(str(k) for k in parsed.keys())
        kind = "mapping"
        if normalized_vendor == "github_actions":
            if "jobs" not in parsed:
                errors.append("GitHub Actions workflow requires top-level 'jobs'")
            if "on" not in parsed and True not in parsed:
                # YAML 1.1 may parse `on:` as boolean True
                warnings.append("GitHub Actions workflow usually has top-level 'on'")
            jobs = parsed.get("jobs")
            if isinstance(jobs, dict) and not jobs:
                warnings.append("'jobs' mapping is empty")
        elif normalized_vendor == "gitlab_ci":
            if "stages" not in parsed and not any(
                isinstance(v, dict) and "script" in v for v in parsed.values()
            ):
                warnings.append(
                    "GitLab CI configs usually define 'stages' or job 'script' blocks"
                )
        elif normalized_vendor == "circleci":
            if "version" not in parsed:
                warnings.append("CircleCI configs usually set 'version: 2.1'")
            if "jobs" not in parsed:
                errors.append("CircleCI config requires top-level 'jobs'")
        elif normalized_vendor == "buildkite":
            if "steps" not in parsed:
                errors.append("Buildkite pipeline requires top-level 'steps'")
            elif not isinstance(parsed.get("steps"), list):
                errors.append("'steps' must be a list")
    elif isinstance(parsed, list):
        kind = "sequence"
        keys = []
        if normalized_vendor == "buildkite":
            # Some Buildkite pipelines are a bare list of steps.
            warnings.append("Bare sequence accepted as Buildkite steps list")

    return {
        "valid": len(errors) == 0,
        "vendor": normalized_vendor,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "keys": keys,
            "kind": kind,
            "top_level_count": len(keys) if kind == "mapping" else (
                len(parsed) if isinstance(parsed, list) else 0
            ),
        },
    }


def record_run(
    *,
    project_id: str,
    vendor: str,
    status: str = "unknown",
    external_id: str | None = None,
    repository: str | None = None,
    ref: str | None = None,
    workflow: str | None = None,
    commit_sha: str | None = None,
    url: str | None = None,
    dedupe_key: str | None = None,
    delivery_run_id: str | None = None,
    deployment_run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Record a pipeline-run provenance entry (BOSS-side ledger, no cloud write).

    Idempotent on ``dedupe_key``: a second call with the same key returns the
    existing record (status/metadata are *not* silently mutated — callers
    should use a new key for updates or record a fresh run).
    """
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("project_id is required")
    vendor_norm = (vendor or "").strip().lower().replace("-", "_")
    if not vendor_norm:
        raise ValueError("vendor is required")
    if vendor_norm in {"gha", "github", "actions"}:
        vendor_norm = "github_actions"
    if vendor_norm in {"gitlab", "gitlabci"}:
        vendor_norm = "gitlab_ci"
    if vendor_norm in {"circle", "circle_ci"}:
        vendor_norm = "circleci"

    status_norm = (status or "unknown").strip().lower()
    if status_norm not in _VALID_RUN_STATUSES:
        raise ValueError(
            f"status must be one of: {', '.join(sorted(_VALID_RUN_STATUSES))}"
        )

    dedupe = (dedupe_key or "").strip() or None
    with _RUN_LOCK:
        if dedupe and dedupe in _DEDUPE:
            existing_id = _DEDUPE[dedupe]
            for run in _RUNS.get(project_id, []):
                if run["id"] == existing_id:
                    return deepcopy(run)
            # Dedupe points at a different project — fall through to create.

        run_id = _make_id("cicd_run")
        now = _now_iso()
        record = {
            "id": run_id,
            "project_id": project_id,
            "vendor": vendor_norm,
            "status": status_norm,
            "external_id": (external_id or "").strip() or None,
            "repository": (repository or "").strip() or None,
            "ref": (ref or "").strip() or None,
            "workflow": (workflow or "").strip() or None,
            "commit_sha": (commit_sha or "").strip() or None,
            "url": (url or "").strip() or None,
            "dedupe_key": dedupe,
            "delivery_run_id": (delivery_run_id or "").strip() or None,
            "deployment_run_id": (deployment_run_id or "").strip() or None,
            "metadata": dict(metadata or {}),
            "actor": (actor or "").strip() or None,
            "recorded_at": now,
            "source": "boss_provenance_ledger",
        }
        bucket = _RUNS.setdefault(project_id, [])
        bucket.insert(0, record)
        # Cap per-project history to keep memory bounded.
        if len(bucket) > 500:
            dropped = bucket[500:]
            del bucket[500:]
            for old in dropped:
                old_key = old.get("dedupe_key")
                if old_key and _DEDUPE.get(old_key) == old["id"]:
                    _DEDUPE.pop(old_key, None)
        if dedupe:
            _DEDUPE[dedupe] = run_id
        return deepcopy(record)


def list_runs(
    *,
    project_id: str,
    limit: int = 50,
    vendor: str | None = None,
) -> dict[str, Any]:
    """List recorded pipeline runs for a project (newest first)."""
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("project_id is required")
    limit = max(1, min(int(limit or 50), 200))
    vendor_norm = (vendor or "").strip().lower().replace("-", "_") or None

    with _RUN_LOCK:
        items = list(_RUNS.get(project_id, []))
    if vendor_norm:
        items = [r for r in items if r.get("vendor") == vendor_norm]
    items = items[:limit]
    return {
        "project_id": project_id,
        "items": deepcopy(items),
        "count": len(items),
    }


def status(*, project_id: str) -> dict[str, Any]:
    """Summary: catalog vendors + recent runs + configuration hints."""
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("project_id is required")

    cat = catalog()
    runs = list_runs(project_id=project_id, limit=20)
    by_vendor: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for run in runs["items"]:
        v = str(run.get("vendor") or "unknown")
        s = str(run.get("status") or "unknown")
        by_vendor[v] = by_vendor.get(v, 0) + 1
        by_status[s] = by_status.get(s, 0) + 1

    recent = runs["items"][:5]
    return {
        "project_id": project_id,
        "vendors": cat["vendors"],
        "supported_generate_targets": cat["supported_generate_targets"],
        "recent_runs": recent,
        "run_counts": {
            "total_listed": runs["count"],
            "by_vendor": by_vendor,
            "by_status": by_status,
        },
        "links": {
            "delivery": "/v1/delivery",
            "deployments": "/v1/deployments",
            "pipeline_adapters": "/v1/pipeline-adapters/catalog",
            "ci_config_generate_legacy": "/v1/ci-config/generate",
        },
        "notes": cat["notes"],
    }


def reset_for_tests() -> None:
    """Clear the in-process provenance ledger (tests only)."""
    with _RUN_LOCK:
        _RUNS.clear()
        _DEDUPE.clear()


# Module-level convenience for callers that prefer a class façade.
class CICDPipelinesService:
    """Thin object façade over the module functions."""

    def catalog(self) -> dict[str, Any]:
        return catalog()

    def generate(
        self,
        *,
        vendor: str,
        language: str,
        framework: Optional[str] = None,
    ) -> dict[str, Any]:
        return generate(vendor=vendor, language=language, framework=framework)

    def validate(
        self,
        yaml_text: str,
        *,
        vendor: Optional[str] = None,
    ) -> dict[str, Any]:
        return validate_yaml_structure(yaml_text, vendor=vendor)

    def record_run(self, **kwargs: Any) -> dict[str, Any]:
        return record_run(**kwargs)

    def list_runs(
        self,
        *,
        project_id: str,
        limit: int = 50,
        vendor: Optional[str] = None,
    ) -> dict[str, Any]:
        return list_runs(project_id=project_id, limit=limit, vendor=vendor)

    def status(self, *, project_id: str) -> dict[str, Any]:
        return status(project_id=project_id)
