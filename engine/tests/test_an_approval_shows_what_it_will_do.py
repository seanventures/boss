"""An approval shows what approving will do, and approves exactly that.

FOUND RUNNING THE BUNDLED EXAMPLES LIVE. The newsletter's send step was held with
"Email the newsletter: weekly_newsletter · run 4715a965", `{environment: "staging", ...}` and
"Approve and retry" / "Keep blocked" — no recipient, no subject, no body. The meeting example's
tickets and the video example's title were just as invisible.

The fix is one module (services/action_preview.py): the step's inputs are resolved as the runner
will resolve them, previewed by a typed builder keyed on the skill id (or a generic fallback), with
credentials hidden, and bound to a content hash so a stale approval cannot send other content.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.services import action_preview as ap
from boss_engine.services import workflow_capability_runner as runner


# ── The context a gate is opened with: start inputs and what earlier steps produced ──────────

NEWSLETTER_RUN = {
    "input": {"title": "E2E: Weekly newsletter", "sectors": "AI, football",
              "to": "reader@example.com", "subject": "Your weekly newsletter"},
    "steps": {"write": {"text": "AI\nModels got cheaper this week (source: The Register).\n"}},
    "skipped": {},
    "workflow_name": "Weekly newsletter",
}

SEND_STEP = {
    "id": "send", "kind": "capability",
    "binding": {"custom_step": "email_the_newsletter",
                "inputs": {"to": {"from_input": "to"},
                           "subject": {"from_input": "subject",
                                       "default": "Your weekly newsletter"},
                           "body": {"from_step": "write", "field": "text"}}},
    "approval": {"mode": "manual"},
}


def _resolved(step, context):
    return runner._resolve_inputs(step["binding"]["inputs"], dict(context))


class TestEachKindReadsItsRealBindings:
    def test_an_email_reads_as_an_email(self):
        inputs = _resolved(SEND_STEP, NEWSLETTER_RUN)
        preview = ap.build("email.send_real_guarded", inputs, step_label="Email the newsletter")
        assert preview["kind"] == "email"
        assert preview["to"] == ["reader@example.com"]
        assert preview["subject"] == "Your weekly newsletter"
        # The FULL body the previous step wrote, not an excerpt or a key.
        assert preview["body"] == NEWSLETTER_RUN["steps"]["write"]["text"]
        assert preview["attachments"] == []
        assert preview["content_hash"] == ap.content_hash(inputs)

    def test_attachments_are_named(self):
        preview = ap.build("email.send_real", {
            "to": "a@example.com, b@example.com", "body": "hi",
            "attachments": [{"filename": "report.pdf"}, "s3://bucket/dir/chart.png"]})
        assert preview["to"] == ["a@example.com", "b@example.com"]
        assert preview["attachments"] == ["report.pdf", "chart.png"]

    def test_tickets_read_as_a_list_from_the_extract_step(self):
        context = {"input": {}, "skipped": {}, "steps": {"extract": {"actions": [
            {"title": "Ship the onboarding fix", "owner": "Priya", "due": "Friday",
             "detail": "Customers drop off at step 3. " * 30},
            {"title": "Book the venue", "owner": None, "detail": "For the offsite."},
        ]}}}
        step = {"binding": {"custom_step": "create_the_tickets",
                            "inputs": {"actions": {"from_step": "extract", "field": "actions"}}}}
        preview = ap.build("tracker.issues.create", _resolved(step, context))
        assert preview["kind"] == "issues"
        assert preview["count"] == 2
        first, second = preview["items"]
        assert (first["title"], first["owner"], first["due"]) == (
            "Ship the onboarding fix", "Priya", "Friday")
        assert len(first["excerpt"]) <= ap.EXCERPT_CHARS + 1 and first["excerpt"].endswith("…")
        assert second["owner"] is None and second["excerpt"] == "For the offsite."

    def test_a_publish_reads_as_a_card(self):
        context = {"input": {"title": "Override title"}, "skipped": {}, "steps": {
            "edit": {"media_artifact_id": "art_1", "title": "From the edit",
                     "description": "A short film about rain."}}}
        step = {"binding": {"custom_step": "publish_to_youtube", "inputs": {
            "media_artifact_id": {"from_step": "edit", "field": "media_artifact_id"},
            "title": {"from_step": "edit", "field": "title", "override_from_input": "title"},
            "description": {"from_step": "edit", "field": "description",
                            "override_from_input": "description"}}}}
        preview = ap.build("publish_youtube", _resolved(step, context))
        assert preview["kind"] == "publish"
        assert preview["platform"] == "YouTube"
        assert preview["title"] == "Override title"
        assert preview["description"] == "A short film about rain."
        assert preview["visibility"] == "private"
        assert preview["media"]["id"] == "art_1"

    def test_any_other_skill_gets_labelled_fields(self):
        preview = ap.build("slack.post_real_guarded",
                           {"channel": "#launch", "max_tokens": 200, "text": "We shipped."})
        assert preview["kind"] == "fields"
        by_name = {f["name"]: f for f in preview["fields"]}
        assert by_name["channel"]["label"] == "Channel"
        assert by_name["max_tokens"]["value"] == "200", "max_tokens is not a credential"
        assert by_name["text"]["value"] == "We shipped."

    def test_a_new_gated_skill_needs_no_code_to_get_a_preview(self):
        assert "some.new_skill" not in ap.PREVIEWERS
        assert ap.build("some.new_skill", {"x": 1})["kind"] == "fields"


class TestSecretsNeverReachAPreview:
    def test_by_key_name(self):
        preview = ap.build("mcp:post", {"api_key": "abc123", "password": "hunter2",
                                        "client_secret": "s", "authorization": "x",
                                        "message": "hello"})
        by_name = {f["name"]: f["value"] for f in preview["fields"]}
        for hidden in ("api_key", "password", "client_secret", "authorization"):
            assert by_name[hidden] == ap.REDACTED
        assert by_name["message"] == "hello"
        assert preview["redactions"] == 4
        assert "hunter2" not in repr(preview)

    def test_by_value_wherever_it_appears(self):
        token = "sk-ant-" + "A1b2C3d4" * 4
        preview = ap.build("mcp:post", {"note": f"use {token} please",
                                        "nested": {"headers": {"x": "Bearer abcdefghijklmnop"}}})
        assert token not in repr(preview) and "abcdefghijklmnop" not in repr(preview)
        assert preview["redactions"] == 2

    def test_inside_an_email_body_too_and_the_hash_still_binds_the_real_content(self):
        inputs = {"to": "a@example.com", "body": "key: ghp_" + "x" * 30}
        preview = ap.build("email.send_real_guarded", inputs)
        assert "ghp_" not in preview["body"] and ap.REDACTED in preview["body"]
        assert preview["redactions"] == 1
        assert preview["content_hash"] == ap.content_hash(inputs)


class TestTheQuestionIsInWords:
    def test_step_label_and_run_title(self):
        title = ap.run_title(NEWSLETTER_RUN["input"], "Weekly newsletter")
        assert ap.gate_question("Email the newsletter", title) == (
            "Email the newsletter — E2E: Weekly newsletter")

    def test_falls_back_to_the_workflow_name_never_the_key(self):
        assert ap.run_title({"sectors": "AI"}, "Weekly newsletter") == "Weekly newsletter"

    def test_the_gate_builds_it_this_way_and_offers_approve_and_reject(self):
        source = inspect.getsource(runner._open_gate)
        assert "gate_question" in source and "open_step_decision(" in source
        assert "· run {run['id'][-8:]}" not in source
        assert '"label": "Approve"' in source and '"label": "Reject"' in source
        # No environment on a step that ships nowhere.
        assert 'in {"deploy", "redeploy", "monitor"}' in source


class _Rows:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Connection:
    """Answers the one query the stale check makes: the decision's recommendation."""

    def __init__(self, recommendation):
        self.recommendation = recommendation

    def execute(self, _sql, params=None):
        return _Rows({"recommendation": self.recommendation})


NOTIFY_STEP = {
    "id": "tell", "kind": "capability",
    "binding": {"capability": "notify",
                "inputs": {"channel": {"from_input": "to"},
                           "message": {"from_step": "write", "field": "text"}}},
}


def _context(text_written: str) -> dict:
    return {"input": {"to": "email:reader@example.com"}, "skipped": {},
            "steps": {"write": {"text": text_written}}, "approval_decision_id": "dec_1"}


class TestAStaleApprovalCannotSendOtherContent:
    def _approved(self, context):
        _skill, inputs = runner._gated_inputs(None, "p", NOTIFY_STEP, context)
        preview = ap.build("capability:notify", inputs, step_label="Tell the team")
        return {"step_label": "Tell the team", "action_preview": preview,
                "approved_content_hash": preview["content_hash"]}

    def test_the_content_that_was_approved_runs(self):
        rec = self._approved(_context("We shipped."))
        assert runner._approved_content_refusal(
            _Connection(rec), "p", NOTIFY_STEP, _context("We shipped.")) is None

    def test_changed_content_is_refused_with_a_sentence(self):
        rec = self._approved(_context("We shipped."))
        said = runner._approved_content_refusal(
            _Connection(rec), "p", NOTIFY_STEP, _context("We shipped — and here is more."))
        assert said and "changed after it was approved" in said
        assert "Nothing was sent" in said and "Retry" in said

    def test_the_step_itself_refuses_before_doing_anything(self):
        rec = self._approved(_context("We shipped."))
        with pytest.raises(runner.StaleApprovalError) as refused:
            runner.execute_step(_Connection(rec), object(), project_id="p", actor="ceo",
                                step=NOTIFY_STEP, context=_context("Different words."))
        assert "changed after it was approved" in str(refused.value)
        # Settled: retrying the same approval would only refuse again.
        assert isinstance(refused.value, runner.SettledCapabilityError)

    def test_a_step_no_gate_released_is_not_checked(self):
        context = _context("anything")
        context.pop("approval_decision_id")
        assert runner._approved_content_refusal(
            _Connection({}), "p", NOTIFY_STEP, context) is None

    def test_a_stale_refusal_clears_the_decision_so_retry_asks_again(self):
        source = "".join(map(inspect.getsource, runner.TICK_PARTS))
        assert "isinstance(error, StaleApprovalError)" in source
        assert "SET decision_id = NULL" in source
