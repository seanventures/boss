"""Every pointer BOSS prints or documents names a console surface that still exists.

Workflow Studio folded the old Agent Studio, Team structure and Agent work into one area — named
**Agent Studio** again on 2026-10-04 (the operator's call), now the agents' one home — and the
Workflow area's Yours / Catalogue / Design a workflow / Your steps tabs into **Workflows → Runs |
Library**. The review of ws/ia (2026-10-03) found the advice had not moved with them:

  * the start checklist's `agent_pool` and per-agent needs said "in Agent Studio" and linked to
    area `studio`, an id the console no longer has — it worked only because of the redirect;
  * the refusal on BOSS's own workflows sent people to "Workflow > Design a workflow" and
    "Workflow > Catalogue";
  * README, Quickstart, the walkthroughs and the examples named "Agent work", "Team structure"
    and "Delivery → Build your own → Your steps", and the install is followed
    literally from the README.

These read the area ids from the console's own list (apps/web/lib/area-ids.ts) and every
document an installer follows, so a surface retired later is caught the same way.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from boss_engine.services import workflow_readiness as ready
from boss_engine.services import workflows

REPO = pathlib.Path(__file__).resolve().parents[2]
SPECPACK = REPO / "packages" / "specpack" / "workflows"

#: Names of console surfaces that no longer exist, as a person would read them.
RETIRED = re.compile(
    r"Team structure|\*\*Agent work\*\*|Build your own|Your steps|"
    r"Design a workflow|Workflow\s*[>→]\s*Catalogue|Delivery\s*→\s*(\*\*)?(Workflows|Yours|Build)|"
    r"Create a new workflow|edit inputs"
)


def _console_area_ids() -> set[str]:
    source = (REPO / "apps" / "web" / "lib" / "area-ids.ts").read_text()
    block = source.split("export const AREA_IDS = [", 1)[1].split("] as const", 1)[0]
    ids = set(re.findall(r'"([a-z_-]+)"', block))
    assert "workforce" in ids and "delivery_workflows" in ids, "area-ids.ts could not be read"
    return ids


def _every_need() -> list[ready.Need]:
    return [*ready.NEEDS.values(), ready._role_need("video_editor"),
            ready._channel_need("email:someone@example.com")]


@pytest.mark.parametrize("need", _every_need(), ids=lambda n: n.key)
def test_every_readiness_fix_links_to_an_area_the_console_has(need):
    if need.area is not None:
        assert need.area in _console_area_ids(), (
            f"{need.key} links to area {need.area!r}, which the console does not have")


@pytest.mark.parametrize("need", _every_need(), ids=lambda n: n.key)
def test_every_readiness_fix_names_a_surface_that_exists(need):
    assert not RETIRED.search(need.fix), need.fix


def test_an_agent_the_workflow_names_is_fixed_in_agent_studio():
    need = ready._role_need("video_editor")
    assert need.area == "workforce"
    assert "Agent Studio (+ New agent)" in need.fix


def test_the_refusal_on_bosss_own_workflows_names_a_surface_that_exists():
    key = sorted(workflows.shipped_workflow_keys(SPECPACK))[0]
    with pytest.raises(workflows.WorkflowNamespaceError) as refused:
        workflows.refuse_operator_edit_of_boss_workflow(
            {"namespace": "system", "workflow_key": key}, SPECPACK)
    message = str(refused.value)
    assert not RETIRED.search(message), message
    assert "Workflows > Library > + New workflow" in message
    assert "Save as my copy" in message


def _install_documents() -> list[pathlib.Path]:
    docs = [REPO / "README.md", *(REPO / "docs" / "community").rglob("*.md"),
            *(REPO / "examples").rglob("*.md"), *(REPO / "examples").rglob("*.py")]
    return sorted(p for p in docs if "node_modules" not in p.parts)


@pytest.mark.parametrize("path", _install_documents(), ids=lambda p: str(p.relative_to(REPO)))
def test_no_install_document_names_a_retired_surface(path):
    hits = [f"{n}: {line.strip()}" for n, line in enumerate(path.read_text().splitlines(), 1)
            if RETIRED.search(line)]
    assert not hits, "\n".join(hits)
