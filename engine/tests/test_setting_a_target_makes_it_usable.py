"""Pointing an app at a host again is how you put it back in service.

HookWatch's deployment target was `status = retired` while bound to the correct host. The
console lists only ACTIVE targets, so the app vanished from every picker — while
`setup-deployment` answered:

    'HookWatch' is already deploying to 'hetzner-ci-01'.

The operator pressed Set target several times, was told each time that it had worked, and
nothing appeared anywhere. The deploy step meanwhile showed a SUCCEEDED deploy whose own detail
read `status=blocked`, for an app answering "not found", with no control to ship it.

Retiring is how an app is taken out of service. Binding it to a host is how it comes back.
Doing the second while silently keeping the first leaves a state nobody asked for and nothing
surfaces.
"""
from __future__ import annotations

import inspect
import re

from apps.api.app import main


class TestBindingRevivesARetiredTarget:
    def _source(self) -> str:
        src = inspect.getsource(main.setup_deployment_for_code_task_endpoint)
        return src

    def test_it_reads_the_status_before_deciding(self):
        """It selected only id and host_id, so retirement was invisible to it."""
        body = self._source()
        assert re.search(r"SELECT id, host_id, status FROM deployment_targets", body)

    def test_a_retired_target_is_set_active_again(self):
        body = self._source()
        assert 'existing["status"] == "retired"' in body
        assert "SET status = 'active'" in body

    def test_both_return_paths_report_the_revival(self):
        """The already-bound path is the one the operator actually hit, and it was the one
        that said nothing."""
        body = self._source()
        assert body.count('"revived": revived') == 2

    def test_the_already_bound_message_changes_when_it_was_revived(self):
        """"already deploying" is a lie about a retired target: it was deploying nowhere."""
        body = self._source()
        assert "is active again on" in body
