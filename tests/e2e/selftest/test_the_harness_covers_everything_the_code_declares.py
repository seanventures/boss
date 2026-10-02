"""The harness's coverage lists come from the code, and these tests hold it to that.

The incident: the console test CLI shipped with seven hard-coded areas (DEFAULT_AREAS in
engine/boss_engine/cli/console_test.py) while the console grew to thirty-six, so the screens
behind R1-R4 — Skills, Agent work, Studio, Workflow — were never walked. And the examples test
asserted the five examples by name while the thing that mattered, whether any of them RUNS, was
never checked. A list someone has to remember to update is a list that shrinks the coverage
silently. Each test below enumerates every instance the product declares and fails if the harness
would skip one.
"""
from __future__ import annotations

import re

from e2e.harness import discover


def test_every_catalogue_template_the_engine_ships_is_driven():
    """discover reads CATALOGUE from source; it must agree with the engine's own import."""
    from boss_engine.services.workflow_catalogue import CATALOGUE

    engine_keys = list(CATALOGUE.keys()) if isinstance(CATALOGUE, dict) else [
        (entry.get("key") if isinstance(entry, dict) else getattr(entry, "key", None))
        for entry in CATALOGUE]
    assert discover.catalogue_keys_from_source() == engine_keys
    assert len(engine_keys) >= 1


def test_every_example_directory_is_driven_and_setup_all_agrees():
    """A new examples/<x>/setup.py is driven without editing the harness; and the harness and
    examples/setup_all.py may never disagree about which examples exist."""
    found = {e.directory for e in discover.examples()}
    on_disk = {p.parent.name for p in discover.EXAMPLES_DIR.glob("*/setup.py")}
    assert found == on_disk
    assert found == set(discover.examples_listed_by_setup_all())


def test_each_example_names_the_workflow_key_its_setup_publishes():
    """drive.py starts `<NAME lowercased, spaces to underscores>`, the slug
    examples/_common.publish creates; a NAME that stops being a plain string breaks this."""
    for example in discover.examples():
        source = example.setup.read_text()
        assert f'NAME = "{example.name}"' in source
        assert re.fullmatch(r"[a-z0-9_]+", example.workflow_key), example


def test_every_console_area_in_areas_is_walked():
    """Counted independently of the reader: every `{ id: "…"` inside the AREAS literal."""
    source = discover.CONSOLE_SHELL.read_text()
    start = source.index("const AREAS: AreaDef[] = [")
    block = re.sub(r"//[^\n]*", "", source[start:source.index("\n];", start)])
    declared = re.findall(r'\bid:\s*"([^"]+)"', block)
    walked = [a.id for a in discover.console_areas()]
    assert walked == declared
    assert {"skills", "studio", "workforce", "delivery_workflows", "decisions"} <= set(walked)


def test_every_static_console_route_is_walked():
    """Every page.tsx outside app/api with no dynamic segment becomes a walked route."""
    routes = set(discover.console_routes())
    for page in discover.WEB_APP.rglob("page.tsx"):
        rel = page.relative_to(discover.WEB_APP).parent
        if any(p.startswith("[") for p in rel.parts) or (rel.parts and rel.parts[0] == "api"):
            continue
        assert ("/" + "/".join(rel.parts)) in routes
    assert "/" in routes and "/v2" in routes
