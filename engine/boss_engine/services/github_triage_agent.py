from __future__ import annotations

import re
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_blocking_decision
from .github_adapter import GitHubAdapter
from .github_community_base import GitHubCommunityAgentBase
from .intake import CommandIntakeService
from .intent_router import IntentRouter


class GitHubTriageAgent(GitHubCommunityAgentBase):
    def __init__(
        self,
        settings: Settings,
        adapter_factory: Optional[Callable[[Settings], GitHubAdapter]] = None,
    ) -> None:
        super().__init__(settings, adapter_factory)
        self.router = IntentRouter(settings)
        self.intake = CommandIntakeService(settings)

    def execute(
        self,
        connection: Connection,
        *,
        project_id: str,
        event_id: str,
        normalized: dict[str, Any],
        job_id: Optional[str],
    ) -> dict[str, Any]:
        sender = normalized.get("sender") or {}
        sender_login = str(sender.get("login") or "")
        if sender_login.casefold() == self.settings.github_app_bot_login.casefold():
            return {"status": "ignored", "reason": "self_sender"}

        existing = connection.execute(
            text(
                """
                SELECT id, task_id, recommendation
                FROM decisions
                WHERE project_id = :project_id
                  AND recommendation->>'source_event_id' = :event_id
                  AND recommendation->>'agent' = 'github.triage'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id, "event_id": event_id},
        ).mappings().first()
        if existing is not None:
            recommendation = existing["recommendation"] or {}
            return {
                "status": "reused",
                "verdict": recommendation.get("verdict"),
                "decision_id": existing["id"],
                "plan_id": recommendation.get("plan_id"),
            }

        issue = normalized.get("issue") or {}
        comment = normalized.get("comment") or {}
        issue_number = int(issue.get("number") or normalized.get("number") or 0)
        if issue_number <= 0:
            raise ValueError("GitHub triage event is missing an issue number.")
        repository = str(normalized.get("repository") or "")
        title = str(issue.get("title") or "").strip()
        body = str(issue.get("body") or "").strip()
        follow_up = str(comment.get("body") or "").strip()
        message = "\n\n".join(item for item in (title, body, follow_up) if item)
        classification = self._classify(message, issue.get("labels") or [])
        verdict = classification["verdict"]
        plan_id = None
        task_id = None
        if verdict == "accept":
            proposed = self.intake.propose_command(
                connection,
                project_id,
                classification["feature_summary"],
                {
                    "source": "github-community",
                    "github_repository": repository,
                    "github_issue_number": issue_number,
                    "source_event_id": event_id,
                    "data_boundary": "internal",
                },
            )
            plan_id = proposed["plan_id"]
            task_id = proposed["task_id"]

        recommendation = {
            "action": (
                "github.community.dispatch_plan"
                if verdict == "accept"
                else "github.community.triage_review"
            ),
            "agent": "github.triage",
            "verdict": verdict,
            "intent": classification["intent"],
            "rationale": classification["rationale"],
            "feature_summary": classification["feature_summary"],
            "missing_info": classification["missing_info"],
            "plan_id": plan_id,
            "github_repository": repository,
            "github_issue_number": issue_number,
            "source_event_id": event_id,
        }
        decision_id = create_blocking_decision(
            connection,
            project_id,
            task_id,
            f"Accept community feature request #{issue_number}?",
            recommendation,
            {"estimated_usd_micros": 0},
            classification["rationale"],
            risk_tier=2,
            default_action="hold",
        )
        comment_body = self._comment_body(
            rationale=classification["rationale"],
            missing_info=classification["missing_info"],
            plan_id=plan_id,
        )
        adapter = self._adapter(repository)
        comment_result, created = self._effect(
            connection,
            project_id=project_id,
            job_id=job_id,
            dedupe_key=f"github:triage-comment:{event_id}",
            effect_kind="github.issue_comment.create",
            effect_scope={"repository": repository, "issue_number": issue_number},
            payload={"body": comment_body, "decision_id": decision_id},
            invoke=lambda: adapter.create_issue_comment(issue_number, comment_body),
        )
        return {
            "status": "completed" if created else "reused",
            "verdict": verdict,
            "decision_id": decision_id,
            "plan_id": plan_id,
            "comment_url": comment_result.get("url"),
        }

    def _classify(self, message: str, labels: list[Any]) -> dict[str, Any]:
        lowered = message.lower()
        normalized_labels = {
            str(label).strip().lower()
            for label in labels
            if isinstance(label, (str, int))
        }
        if normalized_labels & {"spam", "invalid", "off-topic"} or re.search(
            r"\b(buy followers|crypto giveaway|casino|seo services)\b", lowered
        ):
            return self._classification(
                "spam/off-topic", "decline", message, "This appears off-topic or automated."
            )
        if re.search(
            r"\b(bug|crash|exception|regression|broken|fails?|error|incorrect|installation|install|setup)\b",
            lowered,
        ):
            return self._classification(
                "bug_report",
                "maintainer-review",
                message,
                "A maintainer will review the report and request reproduction details if needed.",
            )
        intent_name, _ = self.router.classify_message(message)
        feature_signal = (
            message.lstrip().lower().startswith("[feature]")
            or "community:feature" in normalized_labels
            or intent_name in {"create_code_task", "propose_command", "create_prd"}
        )
        if not feature_signal:
            return self._classification(
                "question_or_feedback",
                "maintainer-review",
                message,
                "A maintainer will review this feedback and respond here.",
            )

        missing: list[str] = []
        if len(message) < 80:
            missing.append("What concrete workflow or outcome should change?")
        if not re.search(
            r"\b(user|customer|maintainer|developer|team|operator|admin|contributor)\b",
            lowered,
        ):
            missing.append("Who benefits from this change, and in which workflow?")
        if not re.search(
            r"\b(should|must|allow|support|when|so that|success|acceptance|done)\b",
            lowered,
        ):
            missing.append("What observable result would make the request complete?")
        if missing:
            result = self._classification(
                "feature_request",
                "needs-info",
                message,
                "The request looks feature-shaped but needs a clearer outcome before planning.",
            )
            result["missing_info"] = missing
            return result
        return self._classification(
            "feature_request",
            "accept",
            message,
            "The request identifies a beneficiary and an observable product outcome.",
        )

    @staticmethod
    def _classification(
        intent: str, verdict: str, message: str, rationale: str
    ) -> dict[str, Any]:
        summary = " ".join(message.split())[:1200]
        return {
            "intent": intent,
            "verdict": verdict,
            "feature_summary": summary,
            "missing_info": [],
            "rationale": rationale,
        }

    @staticmethod
    def _comment_body(
        *,
        rationale: str,
        missing_info: list[str],
        plan_id: Optional[str],
    ) -> str:
        lines = ["Thanks for the feedback.", "", rationale]
        if missing_info:
            lines.extend(["", "Please clarify:"])
            lines.extend(f"- {item}" for item in missing_info)
        if plan_id:
            lines.extend(
                [
                    "",
                    "A draft plan is ready for maintainer review. Work has not started.",
                ]
            )
        return "\n".join(lines)
