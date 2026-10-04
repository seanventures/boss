from __future__ import annotations

import re
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from . import model_routing
from .code_agent import DIFF_REVIEW_SYSTEM_PROMPT
from .core import create_blocking_decision
from .github_adapter import GitHubAdapter
from .github_community_base import GitHubCommunityAgentBase
from .openai_adapter import estimate_cost_usd_micros


_FINDING_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(except\s+(?:Exception\s*)?:\s*pass|catch\s*\([^)]*\)\s*\{\s*\})"),
        "This swallows an error without surfacing, retrying, or escalating it. Please handle it explicitly so failures remain visible.",
    ),
    (
        re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*['\"][^'\"]+['\"]"),
        "This appears to hardcode a credential. Use a SecretRef/configured secret boundary and keep the value out of source control.",
    ),
    (
        re.compile(r"\b(eval|exec)\s*\("),
        "Executing dynamically supplied text is unsafe here. Replace it with an explicit, typed operation boundary.",
    ),
    (
        re.compile(r"\bTODO\b.*\b(auth|security|receipt|budget|tenant|permission)\b", re.IGNORECASE),
        "This leaves a security or governance requirement unfinished in the submitted path. Complete the guard before merge.",
    ),
)


class GitHubPullRequestReviewAgent(GitHubCommunityAgentBase):
    def __init__(
        self,
        settings: Settings,
        adapter_factory: Optional[Callable[[Settings], GitHubAdapter]] = None,
    ) -> None:
        super().__init__(settings, adapter_factory)
        self.budget = BudgetGovernor(settings)

    def execute(
        self,
        connection: Connection,
        *,
        project_id: str,
        event_id: str,
        normalized: dict[str, Any],
        job_id: Optional[str],
    ) -> dict[str, Any]:
        pull_request = normalized.get("pull_request") or {}
        pull_number = int(
            pull_request.get("number") or normalized.get("number") or 0
        )
        if pull_number <= 0:
            raise ValueError("GitHub PR review event is missing a pull-request number.")
        repository = str(normalized.get("repository") or "")
        head_sha = str(pull_request.get("head_sha") or "unknown")
        author = str(pull_request.get("author") or "contributor")
        existing = connection.execute(
            text(
                """
                SELECT id, recommendation
                FROM decisions
                WHERE project_id = :project_id
                  AND recommendation->>'agent' = 'github.pr_review'
                  AND recommendation->>'github_repository' = :repository
                  AND recommendation->>'github_pr_number' = :pull_number
                  AND recommendation->>'head_sha' = :head_sha
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {
                "project_id": project_id,
                "repository": repository,
                "pull_number": str(pull_number),
                "head_sha": head_sha,
            },
        ).mappings().first()
        if existing is not None:
            return {
                "status": "reused",
                "decision_id": existing["id"],
                "value_verdict": (existing["recommendation"] or {}).get(
                    "value_verdict"
                ),
            }

        adapter = self._adapter(repository)
        diff_result, _ = self._effect(
            connection,
            project_id=project_id,
            job_id=job_id,
            dedupe_key=f"github:pr-diff:{repository}:{pull_number}:{head_sha}",
            effect_kind="github.pull_request.diff.read",
            effect_scope={"repository": repository, "pull_request_number": pull_number},
            payload={"head_sha": head_sha},
            invoke=lambda: adapter.get_pull_request_diff(pull_number),
            serialize=lambda value: {"diff": value},
        )
        diff = str(diff_result.get("diff") or "")
        findings = self._review_diff(diff)
        roi = self._roi_recommendation(pull_request, diff, findings)
        model_review = self._model_review(
            connection,
            project_id=project_id,
            job_id=job_id,
            repository=repository,
            pull_number=pull_number,
            head_sha=head_sha,
            pull_request=pull_request,
            diff=diff,
        )
        if model_review:
            roi["rationale"] = str(model_review.get("summary") or roi["rationale"])
            model_verdict = model_review.get("value_verdict")
            if model_verdict in {"worth_reviewing", "needs_revision"}:
                roi["value_verdict"] = model_verdict
            model_notes = model_review.get("roi_notes")
            if isinstance(model_notes, list):
                roi["roi_notes"].extend(str(note) for note in model_notes[:5])
        recommendation = {
            "action": "github.community.merge_authorization",
            "agent": "github.pr_review",
            "value_verdict": roi["value_verdict"],
            "roi_notes": roi["roi_notes"],
            "rationale": roi["rationale"],
            "inline_finding_count": len(findings),
            "github_repository": repository,
            "github_pr_number": pull_number,
            "github_pr_url": pull_request.get("html_url"),
            "head_sha": head_sha,
            "source_event_id": event_id,
        }
        decision_id = create_blocking_decision(
            connection,
            project_id,
            None,
            f"Merge community PR #{pull_number}?",
            recommendation,
            {"estimated_usd_micros": 0},
            roi["rationale"],
            risk_tier=2,
            default_action="hold",
        )

        review_body = self._review_body(findings)
        review_result, review_created = self._effect(
            connection,
            project_id=project_id,
            job_id=job_id,
            dedupe_key=f"github:pr-review:{repository}:{pull_number}:{head_sha}",
            effect_kind="github.pull_request.review.create",
            effect_scope={"repository": repository, "pull_request_number": pull_number},
            payload={
                "head_sha": head_sha,
                "event": "COMMENT",
                "comments": findings,
                "decision_id": decision_id,
            },
            invoke=lambda: adapter.create_review(
                pull_number, review_body, event="COMMENT", comments=findings
            ),
        )
        roi_body = (
            f"@{author} BOSS value/ROI review: **{roi['value_verdict']}**\n\n"
            f"{roi['rationale']}\n\n"
            + "\n".join(f"- {note}" for note in roi["roi_notes"])
            + f"\n\nMaintainer decision: `{decision_id}`. "
            "This agent does not approve or merge the pull request."
        )
        comment_result, comment_created = self._effect(
            connection,
            project_id=project_id,
            job_id=job_id,
            dedupe_key=f"github:pr-roi-comment:{repository}:{pull_number}:{head_sha}",
            effect_kind="github.issue_comment.create",
            effect_scope={"repository": repository, "issue_number": pull_number},
            payload={"body": roi_body, "decision_id": decision_id},
            invoke=lambda: adapter.create_issue_comment(pull_number, roi_body),
        )
        return {
            "status": (
                "completed" if review_created or comment_created else "reused"
            ),
            "decision_id": decision_id,
            "value_verdict": roi["value_verdict"],
            "inline_finding_count": len(findings),
            "review_url": review_result.get("url"),
            "comment_url": comment_result.get("url"),
            "head_sha": head_sha,
            "model_reviewed": bool(model_review),
        }

    def _model_review(
        self,
        connection: Connection,
        *,
        project_id: str,
        job_id: Optional[str],
        repository: str,
        pull_number: int,
        head_sha: str,
        pull_request: dict[str, Any],
        diff: str,
    ) -> Optional[dict[str, Any]]:
        if (
            not self.settings.github_community_model_review_enabled
            or not self.settings.openai_api_key
        ):
            return None
        scope = self._scope(connection, project_id)
        dedupe_key = f"github:model-pr-review:{repository}:{pull_number}:{head_sha}"
        existing = self.receipts.get_by_dedupe_key(
            connection, scope["org_id"], dedupe_key
        )
        if existing and existing.get("status") == "succeeded":
            return existing.get("result") or None
        if existing:
            return None
        amount = self.settings.github_community_model_review_budget_usd_micros
        reservation = self.budget.reserve(
            connection,
            project_id,
            "model",
            amount,
            "Read-only GitHub community pull-request review.",
            20,
        )
        if reservation.get("status") != "reserved":
            return None
        receipt, _ = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "model.github_pr_review",
            {
                "repository": repository,
                "pull_request_number": pull_number,
                "head_sha": head_sha,
                "data_boundary": "internal",
            },
            {"title": pull_request.get("title"), "diff": diff},
            created_by_job_id=job_id,
            data_boundary="internal",
        )
        try:
            response, usage = model_routing.project_chat_adapter(
                connection, self.settings, project_id
            ).create_chat_completion(
                [
                    {"role": "system", "content": DIFF_REVIEW_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"PR title: {pull_request.get('title') or ''}\n"
                            f"PR body: {pull_request.get('body') or ''}\n\n"
                            f"UNIFIED DIFF\n{diff[:120_000]}"
                        ),
                    },
                ]
            )
            raw = str(response.get("content") or "").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
            import json

            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("Model review must return a JSON object.")
            self.receipts.mark_succeeded(
                connection, receipt["id"], usage.get("model"), result
            )
            self.budget.settle(
                connection,
                project_id,
                reservation["reservation_id"],
                receipt["id"],
                "model",
                usage.get("model") or "github-pr-review",
                amount,
                estimate_cost_usd_micros(usage),
                included_in_subscription=model_routing.call_is_included(
                    connection, self.settings, project_id, usage),
            )
            return result
        except Exception as error:  # noqa: BLE001 - deterministic review remains available
            self.receipts.mark_failed(
                connection, receipt["id"], {"message": str(error)[:1000]}
            )
            self.budget.release(
                connection,
                project_id,
                reservation["reservation_id"],
                "Community model review failed; deterministic diff review used.",
            )
            return None

    @staticmethod
    def _review_diff(diff: str) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        path: Optional[str] = None
        new_line: Optional[int] = None
        for raw_line in diff.splitlines():
            if raw_line.startswith("+++ b/"):
                path = raw_line[6:]
                continue
            hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
            if hunk:
                new_line = int(hunk.group(1))
                continue
            if path is None or new_line is None:
                continue
            if raw_line.startswith("-") and not raw_line.startswith("---"):
                continue
            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                added = raw_line[1:]
                for pattern, body in _FINDING_PATTERNS:
                    if pattern.search(added):
                        findings.append(
                            {
                                "path": path,
                                "line": new_line,
                                "side": "RIGHT",
                                "body": body,
                            }
                        )
                        break
                new_line += 1
            elif raw_line.startswith(" "):
                new_line += 1
            if len(findings) >= 20:
                break
        return findings

    @staticmethod
    def _roi_recommendation(
        pull_request: dict[str, Any],
        diff: str,
        findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        added = sum(
            1
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        removed = sum(
            1
            for line in diff.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )
        files = sum(1 for line in diff.splitlines() if line.startswith("diff --git "))
        has_tests = bool(
            re.search(r"(?im)^\+\+\+ b/.*(?:test|spec)[^/]*\.", diff)
            or re.search(r"(?im)^\+\+\+ b/(?:tests?|spec)/", diff)
        )
        draft = bool(pull_request.get("draft"))
        verdict = "needs_revision" if findings or draft else "worth_reviewing"
        notes = [
            f"Change size: {files} file(s), {added} addition(s), {removed} deletion(s).",
            (
                "The diff includes test changes."
                if has_tests
                else "No test-file changes were detected; maintainers should verify coverage."
            ),
            f"Deterministic review found {len(findings)} actionable inline issue(s).",
        ]
        rationale = (
            "The contribution needs revision before a maintainer considers merge."
            if verdict == "needs_revision"
            else "The diff has no obvious blocking defect and is reasonably bounded for maintainer review."
        )
        return {"value_verdict": verdict, "roi_notes": notes, "rationale": rationale}

    @staticmethod
    def _review_body(findings: list[dict[str, Any]]) -> str:
        if findings:
            return (
                f"BOSS reviewed the submitted diff without checking out or executing fork code "
                f"and found {len(findings)} actionable item(s)."
            )
        return (
            "BOSS reviewed the submitted diff without checking out or executing fork code. "
            "No obvious actionable inline defects were found; human review is still required."
        )
