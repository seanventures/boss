"""The app was deployed and answering, and its card offered to deploy it.

    SHIPPED   Interview Scorecard App        3 attempts · last one failed
    NOT DEPLOYED   hetzner-ci-01   [Deploy now]   [Unbind target]

It was up the whole time — `deprun_f948af84` succeeded at 01:50 and the app answers 200 on
192.0.2.202:3015. The card asks "what did this run ship?", and that question is joined on
`release_packet->>'code_task_run_id'`. The deployment carries `crun_7f38987c`, the attempt that
actually succeeded; the card is a different attempt of the same app.

Three attempts, one deployment, and the card asked about the wrong one. Offering "Deploy now"
for something already deployed is not a cosmetic error — pressing it ships again over a
running app.

The join's docstring warned that matching on a NAME risks showing another app's deployment,
and that warning is right. Which is why this matches on IDENTITY within one project — the same
rule the console uses everywhere else to decide two names are one app — rather than on a name
or a LIKE.
"""
from __future__ import annotations

import inspect
import pathlib

from boss_engine.services.naming import identity_key


def _endpoint() -> str:
    """The whole function, by its own source — a byte window silently truncated the host
    lookup and a test then "passed" on half the endpoint."""
    from apps.api.app import main as module

    return inspect.getsource(module.deployments_for_code_task_endpoint)


class TestAnAppsAttemptsAreSeveralRuns:
    def test_the_query_is_no_longer_keyed_on_one_run(self):
        body = _endpoint()
        assert '"WHERE r.release_packet->>\'code_task_run_id\' = :run "' not in body
        assert '"WHERE r.project_id = :p "' in body

    def test_a_deployment_from_this_run_still_matches(self):
        body = _endpoint()
        assert 'r["release_packet_run"] == run_id' in body

    def test_and_so_does_one_from_another_attempt_of_the_same_app(self):
        body = _endpoint()
        assert '_identity_key(str(r["app_name"] or "")) == _wanted' in body

    def test_the_packet_run_is_selected_so_it_can_be_compared(self):
        body = _endpoint()
        assert "r.release_packet->>'code_task_run_id' AS release_packet_run" in body

    def test_and_dropped_before_the_card_sees_it(self):
        """An internal join key is not part of the card's contract."""
        body = _endpoint()
        assert 'item.pop("release_packet_run", None)' in body

    def test_the_lookup_stays_inside_one_project(self):
        body = _endpoint()
        assert "_project_of_run(run_id)" in body

    def test_the_limit_still_applies_after_filtering(self):
        """Scanning wider must not hand the card two hundred rows."""
        body = _endpoint()
        assert "][:max(1, min(int(limit), 20))]" in body


class TestTheHostLookupWasWorseThanItLooked:
    def test_it_no_longer_compares_names_literally(self):
        body = _endpoint()
        assert '"WHERE t.app_name = :a AND t.status = \'active\' "' not in body

    def test_a_name_with_a_unicode_hyphen_still_finds_its_target(self):
        """As actually stored: "Lead‐Triage Inbox" carries U+2010, which never equals the ASCII
        name the console holds."""
        assert "Lead‐Triage Inbox" != "Lead-Triage Inbox"
        assert identity_key("Lead‐Triage Inbox") == identity_key("Lead-Triage Inbox")

    def test_and_it_cannot_reach_into_another_project(self):
        body = _endpoint()
        assert '"WHERE t.project_id = :p AND t.status = \'active\' "' in body

    def test_two_different_apps_still_do_not_match(self):
        assert identity_key("Interview Scorecard App") != identity_key("Change Calendar")


class TestTheHelperItLeansOn:
    def test_the_project_of_a_run_is_looked_up_once(self):
        main = pathlib.Path("apps/api/app/main.py").read_text()
        assert "def _project_of_run(run_id: str) -> str:" in main
        assert "SELECT project_id FROM code_task_runs WHERE id = :r" in main

    def test_a_missing_run_yields_no_project_rather_than_raising(self):
        """A card for a run that has been cleaned up should render empty, not 500."""
        main = pathlib.Path("apps/api/app/main.py").read_text()
        i = main.index("def _project_of_run")
        assert 'or ""' in main[i:i + 400]
