"""Inbound Plane webhook → auto code-task (Phase 1 of the Plane↔BOSS integration).

A human files a Plane issue and labels it ``boss:coding`` / ``boss:bug`` / ``boss:design``;
Plane fires a signed ``issue`` webhook at ``/v1/webhooks/plane``. This service:

  1. verifies the ``X-Plane-Signature`` HMAC (fail-closed — an unsigned/mismatched body is rejected),
  2. filters — only a *watched project* + a ``boss:`` label + ``action == created`` ever starts a job
     (updates are ignored so BOSS's own status writes in Phase 2 can't loop),
  3. maps the issue → :func:`start_code_task` (repo from the project→repo map),
  4. dedups on ``plane_issue_id`` so a redelivery or edit never spawns a second job.

Design note: Plane signs each delivery with a hex HMAC-SHA256 of the raw body under
``X-Plane-Signature`` — a *different* scheme from GitHub's ``x-hub-signature-256`` (``sha256=`` prefix),
so this can't reuse ``InboundWebhookService.ingest`` as-is; it reuses only the hmac primitives.
The exact payload field names are confirmed against the first real delivery (spec §8) — the parser is
deliberately tolerant (labels may arrive as strings, ``{name}`` dicts, or bare ids) and logs what it saw.

See docs/infra/BOSS_PLANE_INTEGRATION_SPEC.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import urllib.request
from secrets import compare_digest
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .code_task_service import start_code_task

logger = logging.getLogger(__name__)

# boss:<type> labels that route to the code-task pipeline in v1. boss:design is watched (so it is
# not silently dropped) but deferred to the Phase-3 PRD/design flow rather than run as a code task.
_CODE_TASK_LABELS = {"boss:coding", "boss:bug"}
_DESIGN_LABEL = "boss:design"
# P3 (#116): Plane priority -> BOSS risk_tier hint (higher tier = more rigor/gates). Unknown -> default.
_PRIORITY_RISK = {"urgent": 3, "high": 3, "medium": 2, "low": 2}


def _frame_instructions(description: str, *, is_bug: bool = False) -> str:
    """P3: a boss:bug ticket is framed as a bug — reproduce → root-cause → fix → regression test —
    rather than a plain 'implement this'. boss:coding passes through unchanged."""
    if is_bug:
        return ("BUG REPORT — reproduce the issue, find the root cause, fix it, and add a regression "
                "test that FAILS before your fix and passes after.\n\n" + description)
    return description


class PlaneWebhookService:
    """Authenticate + route a Plane issue webhook into the code-task pipeline."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._label_cache: dict[str, dict[str, str]] = {}   # project_id -> {label_id: name}

    # --- config helpers -------------------------------------------------------
    def _watch_labels(self) -> set[str]:
        return {v.lower() for v in self.settings.plane_watch_label_values}

    def _project_map(self) -> dict[str, dict[str, str]]:
        """{plane_project_id: {"owner","repo","project_id"?}} parsed defensively — a malformed
        value yields an empty map (fail-closed: nothing routes) rather than crashing the request."""
        raw = (self.settings.plane_project_map or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("PLANE_PROJECT_MAP is not valid JSON; no projects will route")
            return {}
        if not isinstance(parsed, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for pid, entry in parsed.items():
            if isinstance(entry, dict):
                out[str(pid)] = {str(k): str(v) for k, v in entry.items()}
        return out

    # --- label resolution (Plane sends label UUIDs in the webhook, not names) -
    _UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def _api_ctx(self) -> tuple[str, str, str]:
        """(base_url, workspace, token) for the Plane REST API — from config, deriving base+workspace
        from PLANE_PROJECT_URL when PLANE_BASE_URL isn't set."""
        base = (self.settings.plane_base_url or "").rstrip("/")
        ws = getattr(self.settings, "plane_workspace", "") or ""
        proj_url = self.settings.plane_project_url or ""
        if (not base or not ws) and proj_url:
            m = re.match(r"(https?://[^/]+)/([^/]+)/", proj_url)
            if m:
                base = base or m.group(1)
                ws = ws or m.group(2)
        return base, (ws or "default"), (self.settings.plane_api_token or "")

    def _project_labels(self, project_id: str, *, force: bool = False) -> dict[str, str]:
        """Cached {label_id: name} for a project, fetched from the Plane REST API. Empty on any
        failure (the issue then simply won't match a watch label — fail-safe, not a crash)."""
        if not force and project_id in self._label_cache:
            return self._label_cache[project_id]
        base, ws, token = self._api_ctx()
        if not (base and token and project_id):
            return {}
        url = f"{base}/api/v1/workspaces/{ws}/projects/{project_id}/labels/"
        try:
            req = urllib.request.Request(url, headers={"X-API-Key": token})
            from .plane_client import plane_ssl_context
            with urllib.request.urlopen(req, timeout=10, context=plane_ssl_context(self.settings)) as r:
                data = json.load(r)
            rows = data.get("results", data) if isinstance(data, dict) else data
            m = {str(x["id"]): str(x["name"]) for x in rows
                 if isinstance(x, dict) and "id" in x and "name" in x}
            self._label_cache[project_id] = m
            return m
        except Exception as exc:  # noqa: BLE001
            logger.warning("plane: could not fetch labels for project %s: %s", project_id, exc)
            return {}

    def _resolve_labels(self, project_id: Optional[str], raw: set[str]) -> set[str]:
        """Map any label *UUIDs* in a webhook payload to their names (Plane sends ids, we watch by
        name). Names pass through unchanged. Refetches once if a uuid misses the cache (new label)."""
        uuids = {x for x in raw if self._UUID.match(x)}
        if not uuids or not project_id:
            return raw
        id2name = self._project_labels(project_id)
        if uuids - id2name.keys():                       # a new label the cache predates -> refetch
            id2name = self._project_labels(project_id, force=True)
        return {id2name.get(x, x).lower() for x in raw}

    # --- signature ------------------------------------------------------------
    def verify_signature(self, raw_body: bytes, signature: str) -> bool:
        secret = (self.settings.plane_webhook_secret or "").strip()
        if not secret or not signature:
            return False
        digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        # Plane sends a bare hex digest; tolerate a ``sha256=`` prefix in case the instance adds one.
        candidate = signature.strip()
        if candidate.startswith("sha256="):
            candidate = candidate[len("sha256="):]
        return compare_digest(candidate, digest)

    # --- payload parsing ------------------------------------------------------
    @staticmethod
    def _labels_of(issue: dict[str, Any]) -> set[str]:
        """Extract label *names* from an issue, tolerant of Plane's delivery shapes: a list of
        strings, a list of ``{"name": ...}`` dicts, or (unhelpfully) a list of bare label ids. Bare
        ids can't be matched to ``boss:*`` names without an API lookup, so they simply won't match."""
        labels = issue.get("labels") or issue.get("label_details") or []
        names: set[str] = set()
        if isinstance(labels, list):
            for item in labels:
                if isinstance(item, str):
                    names.add(item.lower())
                elif isinstance(item, dict):
                    name = item.get("name")
                    if isinstance(name, str):
                        names.add(name.lower())
        return names

    def parse(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Flatten a Plane webhook envelope into the fields we route on. Tolerant of the issue
        living at the top level or under ``data`` (Plane nests the entity under ``data``)."""
        action = str(payload.get("action") or "").strip().lower()
        issue = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        issue = issue or {}
        project = issue.get("project")
        if isinstance(project, dict):
            project = project.get("id")
        return {
            "action": action,
            "event": str(payload.get("event") or "").strip().lower(),
            "plane_issue_id": str(issue.get("id") or "").strip() or None,
            "plane_project_id": str(project or "").strip() or None,
            "sequence_id": issue.get("sequence_id"),
            "title": (issue.get("name") or "").strip(),
            # description_html is the rich body; description_stripped is plain text — prefer plain.
            "description": (
                issue.get("description_stripped")
                or issue.get("description")
                or issue.get("description_html")
                or ""
            ).strip(),
            "priority": issue.get("priority"),
            "labels": self._labels_of(issue),
        }

    def route(self, parsed: dict[str, Any]) -> dict[str, Any]:
        """Decide accept vs ignore (with a reason) from the parsed fields — pure, so it's unit-testable
        without a DB. Does NOT check dedup (that needs a connection)."""
        if parsed["event"] and parsed["event"] != "issue":
            return {"decision": "ignore", "reason": f"event is {parsed['event']!r}, not issue"}
        if parsed["action"] != "created":
            return {"decision": "ignore", "reason": f"action is {parsed['action']!r}, not created"}
        if not parsed["plane_issue_id"]:
            return {"decision": "ignore", "reason": "no issue id in payload"}
        pmap = self._project_map()
        if parsed["plane_project_id"] not in pmap:
            return {"decision": "ignore", "reason": "project not in the watch/repo map"}
        watch = self._watch_labels()
        matched = parsed["labels"] & watch
        if not matched:
            return {"decision": "ignore", "reason": "no boss: label on the issue"}
        entry = pmap[parsed["plane_project_id"]]
        if matched == {_DESIGN_LABEL}:
            # boss:design → convene a governed PRD meeting (agent panel → PRD →
            # human approval → auto-started code task), not a v1 code task.
            return {
                "decision": "accept",
                "mode": "meeting",
                "label": _DESIGN_LABEL,
                "project_id": entry.get("project_id") or self.settings.default_project_id,
                "owner": entry.get("owner") or self.settings.target_github_owner,
                "repo": entry.get("repo") or self.settings.target_github_repo,
            }
        code_labels = matched & _CODE_TASK_LABELS
        if not code_labels:
            return {"decision": "ignore", "reason": f"no code-task label among {sorted(matched)}"}
        label = sorted(code_labels)[0]
        return {
            "decision": "accept",
            "mode": "code",
            "label": label,
            "is_bug": label == "boss:bug",                       # P3: bug -> repro-seeded framing
            "risk_tier": _PRIORITY_RISK.get(str(parsed.get("priority") or "").lower()),  # P3: priority hint
            "owner": entry.get("owner") or self.settings.target_github_owner,
            "repo": entry.get("repo") or self.settings.target_github_repo,
            "project_id": entry.get("project_id") or self.settings.default_project_id,
        }

    # --- orchestration --------------------------------------------------------
    def _existing_run(self, connection: Connection, project_id: str, plane_issue_id: str) -> Optional[str]:
        return connection.execute(
            text(
                "SELECT id FROM code_task_runs "
                "WHERE project_id = :p AND finalize_ctx->>'plane_issue_id' = :iid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"p": project_id, "iid": plane_issue_id},
        ).scalar_one_or_none()

    def _existing_meeting(self, connection: Connection, project_id: str, plane_issue_id: str) -> Optional[str]:
        return connection.execute(
            text(
                "SELECT id FROM meeting_runs "
                "WHERE project_id = :p AND constraints->>'plane_issue_id' = :iid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"p": project_id, "iid": plane_issue_id},
        ).scalar_one_or_none()

    def _convene_meeting_for_issue(
        self, connection: Connection, parsed: dict[str, Any],
        decision: dict[str, Any], jobs,
    ) -> dict[str, Any]:
        """boss:design → create a task and convene a governed PRD meeting. The
        meeting's PRD-approval then auto-starts the code task (decisions
        follow-on), completing Plane → meeting → approval → coding. Idempotent."""
        existing = self._existing_meeting(
            connection, decision["project_id"], parsed["plane_issue_id"]
        )
        if existing:
            return {"status": "duplicate", "mode": "meeting", "run_id": existing,
                    "plane_issue_id": parsed["plane_issue_id"]}
        from .budget import BudgetGovernor
        from .intake import CommandIntakeService
        from .meeting_service import convene_prd_meeting_core

        title = parsed["title"] or f"Plane issue {parsed['sequence_id'] or parsed['plane_issue_id']}"
        directive = f"{title}\n\n{parsed['description']}" if parsed.get("description") else title
        result = convene_prd_meeting_core(
            connection,
            settings=self.settings,
            budget=BudgetGovernor(self.settings),
            intake=CommandIntakeService(self.settings),
            jobs=jobs,
            project_id=decision["project_id"],
            directive=directive,
            constraints={
                "source": "plane",
                "plane_issue_id": parsed["plane_issue_id"],
                "plane_project_id": parsed["plane_project_id"],
                "plane_sequence_id": parsed["sequence_id"],
            },
            role_keys=[],
        )
        logger.info(
            "plane webhook accepted (meeting): issue=%s (#%s) -> meeting_run=%s task=%s",
            parsed["plane_issue_id"], parsed["sequence_id"],
            result["run_id"], result["task_id"],
        )
        return {"status": "accepted", "mode": "meeting", "run_id": result["run_id"],
                "task_id": result["task_id"],
                "plane_issue_id": parsed["plane_issue_id"], "label": decision["label"]}

    def ingest(
        self, connection: Connection, raw_body: bytes, headers: dict[str, str], jobs
    ) -> dict[str, Any]:
        """Verify → filter → map → start_code_task. Returns a status dict the endpoint maps to HTTP.

        status ∈ {not_configured, rejected, invalid, ignored, duplicate, accepted}.
        """
        if not self.settings.plane_integration_enabled:
            return {"status": "not_configured", "reason": "PLANE_INTEGRATION_ENABLED is off"}
        if not (self.settings.plane_webhook_secret or "").strip():
            return {"status": "not_configured", "reason": "PLANE_WEBHOOK_SECRET is unset"}

        signature = headers.get("x-plane-signature", "")
        if not self.verify_signature(raw_body, signature):
            return {"status": "rejected", "reason": "signature mismatch"}

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"status": "invalid", "reason": "body is not JSON"}
        if not isinstance(payload, dict):
            return {"status": "invalid", "reason": "payload is not an object"}

        parsed = self.parse(payload)
        # Plane's webhook carries label UUIDs, not names — resolve them so the boss: watch labels match.
        parsed["labels"] = self._resolve_labels(parsed["plane_project_id"], parsed["labels"])
        decision = self.route(parsed)
        if decision["decision"] != "accept":
            logger.info(
                "plane webhook ignored (issue=%s action=%s labels=%s): %s",
                parsed["plane_issue_id"], parsed["action"], sorted(parsed["labels"]),
                decision["reason"],
            )
            return {"status": "ignored", "reason": decision["reason"],
                    "plane_issue_id": parsed["plane_issue_id"]}

        # boss:design → convene a governed PRD meeting instead of a direct code task.
        # (The meeting's PRD-approval then auto-starts the code task.)
        if decision.get("mode") == "meeting":
            return self._convene_meeting_for_issue(connection, parsed, decision, jobs)

        # Idempotency: one Plane issue → at most one run. A redelivery/edit returns the same run.
        existing = self._existing_run(connection, decision["project_id"], parsed["plane_issue_id"])
        if existing:
            return {"status": "duplicate", "run_id": existing,
                    "plane_issue_id": parsed["plane_issue_id"]}

        title = parsed["title"] or f"Plane issue {parsed['sequence_id'] or parsed['plane_issue_id']}"
        instructions = _frame_instructions(parsed["description"] or title, is_bug=decision.get("is_bug"))
        run_id = start_code_task(
            connection,
            project_id=decision["project_id"],
            task_id=None,
            title=title,
            instructions=instructions,
            acceptance_criteria=[],
            test_commands=[],
            owner=decision["owner"],
            repo=decision["repo"],
            base_branch="main",
            risk_tier=decision.get("risk_tier"),                  # P3: priority -> risk hint
            finalize_ctx={
                "source": "plane",
                "plane_issue_id": parsed["plane_issue_id"],
                "plane_project_id": parsed["plane_project_id"],
                "plane_sequence_id": parsed["sequence_id"],
                "plane_label": decision["label"],
            },
            jobs=jobs,
        )
        logger.info(
            "plane webhook accepted: issue=%s (#%s) -> run=%s repo=%s/%s label=%s",
            parsed["plane_issue_id"], parsed["sequence_id"], run_id,
            decision["owner"], decision["repo"], decision["label"],
        )
        # P2 (#115): immediately move the Plane ticket to In Progress + comment that BOSS picked it up.
        # Best-effort — a Plane API hiccup must not fail the accepted webhook.
        try:
            from . import plane_sync
            from .plane_client import PlaneClient
            run_stub = {"id": run_id, "project_id": decision["project_id"],
                        "finalize_ctx": {
                "source": "plane", "plane_issue_id": parsed["plane_issue_id"],
                "plane_project_id": parsed["plane_project_id"]}}
            result = plane_sync.sync_run(
                PlaneClient(self.settings), run_stub, "code_task.started",
                connection=connection,
            )
            if not result["synced"]:
                logger.warning(
                    "plane: 'started' sync needs reconciliation for issue %s: %s",
                    parsed["plane_issue_id"], result.get("receipt_ids"),
                )
        except Exception:  # noqa: BLE001
            logger.warning("plane: could not sync 'started' for issue %s", parsed["plane_issue_id"])
        return {"status": "accepted", "run_id": run_id,
                "plane_issue_id": parsed["plane_issue_id"], "label": decision["label"]}
