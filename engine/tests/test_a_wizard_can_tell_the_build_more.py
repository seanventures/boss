"""Starting a project could say what to build. It could not say where, or against what.

The New Project form was one textarea and a workflow dropdown. Which repository the code went
in was resolved silently from the deployment's global default, the acceptance criteria were
whatever a panel later proposed, and there was no way at all to say "read this API doc first".

The capability already declared `repository` optional and `create_code_task` already resolved
one through the project's registry — the wiring existed and nothing upstream could reach it.
Three gaps closed here:

  1. An OPTIONAL input bound from the run's input made that input MANDATORY, because
     `from_input` raised on a missing key while `from_step` had always skipped. So binding
     anything a wizard collects would have broken every run started from a chat, a catalogue
     card or a sample.
  2. The catalogue's build steps never read those inputs, so supplying them changed nothing.
  3. Reference links had nowhere to go at all.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.services import workflow_catalogue as cat
from boss_engine.services.platform_standards import (
    _REFERENCES,
    load_reference_links,
    reference_material,
)
from boss_engine.services.workflow_capabilities import CAPABILITIES
from boss_engine.services.workflow_capability_runner import CapabilityExecutionError


def _resolve(inputs, context, optional=()):
    """The runner's own input resolution, exercised the way a step exercises it."""
    from boss_engine.services.workflow_capability_runner import _resolve_inputs

    return _resolve_inputs(inputs, context, optional=set(optional))


class TestAnOptionalInputThatWasNotSupplied:
    """The rule `from_step` has always had, and `from_input` did not."""

    def test_is_simply_absent(self):
        out = _resolve({"repository": {"from_input": "repository"}},
                       {"input": {"title": "x"}}, optional=["repository"])
        assert out == {}

    def test_a_required_one_still_fails_loudly(self):
        """A build with no instructions must not quietly run on nothing."""
        with pytest.raises(CapabilityExecutionError) as err:
            _resolve({"instructions": {"from_input": "directive"}}, {"input": {}})
        assert "directive" in str(err.value)

    def test_a_supplied_optional_is_passed_through(self):
        out = _resolve({"repository": {"from_input": "repository"}},
                       {"input": {"repository": "paramg/boss-sandbox"}},
                       optional=["repository"])
        assert out == {"repository": "paramg/boss-sandbox"}


class TestEveryBuildStepCanAcceptWhatTheWizardCollects:
    def test_the_build_capability_declares_them_optional(self):
        build = CAPABILITIES["build"]
        for field in ("repository", "acceptance_criteria", "reference_links"):
            assert field in build.optional_inputs, field
        # And they are NOT required: one catalogue has to serve both the wizard and a one-line
        # directive typed into a chat.
        for field in ("repository", "acceptance_criteria", "reference_links"):
            assert field not in build.required_inputs, field

    def test_every_catalogue_build_step_reads_them(self):
        """Binding it on one workflow and not the others is how "it worked when I used the
        standard path" happens."""
        seen = 0
        for entry in cat.CATALOGUE:
            for step in entry["steps"]:
                binding = step.get("binding") or {}
                if binding.get("capability") != "build":
                    continue
                seen += 1
                inputs = binding.get("inputs") or {}
                assert inputs.get("repository") == {"from_input": "repository"}, entry["key"]
                assert inputs.get("acceptance_criteria") == {"from_input": "acceptance_criteria"}
                assert inputs.get("reference_links") == {"from_input": "reference_links"}
        assert seen >= 8, "expected the catalogue's build steps to be covered"

    def test_the_work_itself_is_still_bound(self):
        """The additions must not have displaced what a build actually needs."""
        for entry in cat.CATALOGUE:
            for step in entry["steps"]:
                binding = step.get("binding") or {}
                if binding.get("capability") != "build":
                    continue
                inputs = binding["inputs"]
                assert inputs["title"] == {"from_input": "title"}
                assert inputs["instructions"] == {"from_input": "directive"}


class TestReferenceMaterialReachesTheBuilder:
    @pytest.fixture(autouse=True)
    def _clean(self):
        token = _REFERENCES.set(())
        yield
        _REFERENCES.reset(token)

    def test_nothing_is_said_when_nothing_was_given(self):
        """An empty heading in every prompt trains agents to skim the section."""
        assert load_reference_links([]) == 0
        assert reference_material() == ""

    def test_the_links_are_listed(self):
        load_reference_links(["https://docs.example.com/api"])
        assert "https://docs.example.com/api" in reference_material()

    def test_blank_entries_are_dropped(self):
        assert load_reference_links(["https://a.example", "", "   "]) == 1

    def test_it_says_to_open_them_rather_than_implying_they_are_indexed(self):
        """Nothing embeds or searches these. Describing them as a knowledge base would promise
        retrieval that does not exist, and the operator would size their expectations to the
        promise rather than to the behaviour."""
        load_reference_links(["https://docs.example.com/api"])
        block = reference_material()
        assert "not indexed" in block
        assert "fetch" in block.lower()

    def test_an_unreachable_link_must_be_reported_not_imagined(self):
        """A build that invents the contents of a doc it could not open is worse than one that
        says it could not open it — the invention is indistinguishable from research."""
        load_reference_links(["https://docs.example.com/api"])
        assert "do not invent" in reference_material().lower()

    def test_it_rides_in_the_block_every_stage_shares(self):
        """Material read only at design time is forgotten by the time code is written."""
        from boss_engine.services import governed_code_task_service as svc

        body = inspect.getsource(svc._ctx_block)
        assert "reference_material()" in body
        assert "{references}" in body

    def test_the_run_loads_them_from_its_own_spec(self):
        from boss_engine.services import governed_code_task_service as svc

        body = inspect.getsource(svc.run_governed_code_task)
        assert "load_reference_links" in body
        assert 'spec or {}).get("reference_links")' in body


class TestTheChainFromToolToSpec:
    def test_create_code_task_accepts_them(self):
        from boss_engine.tools.builtin import build_default_registry

        spec = {s.name: s for s in build_default_registry().specs()}["create_code_task"]
        props = (spec.parameters or {}).get("properties") or {}
        assert "reference_links" in props
        assert "repository" in props

    def test_and_hands_them_to_the_run(self):
        from boss_engine.tools import builtin

        body = inspect.getsource(builtin)
        assert 'reference_links=args.get("reference_links") or []' in body

    def test_the_spec_carries_them_so_a_relaunch_keeps_them(self):
        from boss_engine.services import code_task_service as cts

        body = inspect.getsource(cts.start_code_task)
        assert '"reference_links"' in body
