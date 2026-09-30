"""Where an app is bound is the fact everything downstream depends on, and it was not returned.

`host_id` decides three things: the ssh credential resolves THROUGH it, the host panel counts
its apps BY it, and a deploy without it is refused. The deployment-target API did not serialise
it at all — so the console could not show where an app was bound, the agent could not read it,
and `target.get("host_id")` came back None whether the app was unbound or the field was simply
missing.

Those two are not the same thing and the difference is expensive. In one session it produced a
wrong diagnosis: two targets read as unbound while the host panel — which queries the column
directly — listed one of them as bound to it. Two screens disagreeing about one fact, again,
because one of them was never told.
"""
from __future__ import annotations

import inspect

from boss_engine.schemas import DeploymentTargetSummary
from boss_engine.services import resource_plane


class TestTheApiCarriesTheBinding:
    def test_the_summary_model_has_host_id(self):
        assert "host_id" in DeploymentTargetSummary.model_fields

    def test_unbound_is_expressible(self):
        """An app with no host is a real state, so the field is optional, not required."""
        assert DeploymentTargetSummary.model_fields["host_id"].default is None

    def test_the_list_query_selects_it(self):
        src = inspect.getsource(resource_plane.ResourcePlaneService.list_deployment_targets)
        assert "host_id" in src

    def test_the_single_read_selects_it_too(self):
        """A field present in the list and missing from the detail is worse than absent from
        both: it makes the two views disagree."""
        src = inspect.getsource(resource_plane.ResourcePlaneService._get_deployment_target)
        assert "host_id" in src
