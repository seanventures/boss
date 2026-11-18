"""The factory template catalog is a directory of specs, not a database.

BOSSAPP-1276. The launch path builds whatever these files describe, so a file that adds a field,
drops one, repeats an id, or lets web, android, maps, game, 3d, or xr fall under three templates
fails here. This test only reads the YAML.

Stranger test 2026-11-18: the gallery offered three Android (Jetpack Compose) templates and a
Godot game. No BOSS image carries an Android SDK, an emulator or a game engine, so they could
never build. Every template now names the toolchain it builds with, its stack must be one that
toolchain builds, and no template may need a platform the image does not carry.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import yaml

CATALOG = Path(__file__).resolve().parents[1] / "boss_engine" / "data" / "factory_templates"

# The field set is the contract. A new key is a new catalog, not a quiet extra.
FIELDS = ("id", "title", "blurb", "category", "prompt", "stack", "toolchain", "resources",
          "workflows")

CATEGORIES = {"web", "maps", "game", "3d", "xr", "data", "internal"}

# The five a launch showcase has to be able to start. data and internal may fill the rest.
MINIMUMS = {"web": 3, "maps": 3, "game": 3, "3d": 3, "xr": 3}

FIRST_CATALOG = 21

# What each toolchain builds (Python and Node are in every image; Go and Java are opt-in). A
# template's stack must be one its toolchain builds: a "Go" stack declared `node` would be offered
# on an install that cannot build it.
STACKS_BY_TOOLCHAIN = {
    "python": {"Python", "DuckDB"},
    "node": {"Next.js", "Three.js", "WebXR", "MapLibre GL", "Leaflet", "Phaser", "HTML canvas"},
    "go": {"Go", "Go API"},
    "java": {"Java", "Spring Boot"},
}

# Platforms no BOSS image carries. A template that needs one can never build.
UNBUILDABLE = re.compile(
    r"\b(android|jetpack|compose|kotlin|ios|swift|xcode|emulator|godot|unity|unreal|"
    r"react native|flutter|expo|apk|play store|app store)\b", re.I)

# Resource strings name a kind the launch path can attach locally. An account id is not a kind.
RESOURCE_KINDS = ("repo", "preview", "database", "model")

# Real step names, not slogans. Every template the launch builds can be scaffolded, tested, and previewed.
REQUIRED_STEPS = {"scaffold", "test", "preview"}

_KEBAB = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_STEP = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
_STACK = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+#/ -]{0,30}[A-Za-z0-9]")


def _yaml_files() -> list[Path]:
    if not CATALOG.is_dir():
        return []
    return sorted(path for path in CATALOG.glob("*.yaml") if path.is_file())


def _one_sentence(text: str) -> bool:
    body = text.strip()
    if not body or body != text or "\n" in body:
        return False
    if body[-1] not in ".!?":
        return False
    return sum(body.count(mark) for mark in ".!?") == 1


def _problems(path: Path, doc: object) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict):
        return [f"{path.name} did not parse to a mapping"]
    keys = list(doc)
    if set(keys) != set(FIELDS) or len(keys) != len(FIELDS):
        problems.append(f"fields are {keys}, want exactly {list(FIELDS)}")
        return problems

    ident = doc["id"]
    if not isinstance(ident, str) or _KEBAB.fullmatch(ident) is None or not 3 <= len(ident) <= 48:
        problems.append(f"id {ident!r} is not kebab-case of 3 to 48 characters")
    elif ident != path.stem:
        problems.append(f"id {ident!r} does not match the file name {path.stem}")

    title = doc["title"]
    if not isinstance(title, str) or not title.strip() or "\n" in title or len(title) > 80:
        problems.append(f"title {title!r} is not a single short line")

    blurb = doc["blurb"]
    if not isinstance(blurb, str) or not _one_sentence(blurb) or not 8 <= len(blurb.split()) <= 32:
        problems.append(f"blurb {blurb!r} is not one sentence")

    category = doc["category"]
    if category not in CATEGORIES:
        problems.append(f"category {category!r} is not one of {sorted(CATEGORIES)}")

    prompt = doc["prompt"]
    if not isinstance(prompt, str):
        problems.append("prompt is not text")
    else:
        words = re.findall(r"[A-Za-z0-9']+", prompt)
        if len(words) < 40 or prompt.count(".") < 3:
            problems.append("prompt is a slogan, not a spec a builder can execute")
        if isinstance(blurb, str) and prompt.strip() == blurb.strip():
            problems.append("prompt repeats the blurb")

    stack = doc["stack"]
    if not isinstance(stack, str) or _STACK.fullmatch(stack) is None:
        problems.append(f"stack {stack!r} is not a short stack name")

    toolchain = doc["toolchain"]
    if toolchain not in STACKS_BY_TOOLCHAIN:
        problems.append(f"toolchain {toolchain!r} is not one of {sorted(STACKS_BY_TOOLCHAIN)}")
    elif stack not in STACKS_BY_TOOLCHAIN[toolchain]:
        problems.append(f"stack {stack!r} is not one the {toolchain} toolchain builds")

    resources = doc["resources"]
    wanted = [kind for kind in RESOURCE_KINDS if isinstance(resources, list) and kind in resources]
    if resources != wanted or not resources:
        problems.append(
            f"resources {resources!r} must be a non-empty subset of {list(RESOURCE_KINDS)} "
            "in that order, with no account ids")

    workflows = doc["workflows"]
    steps_seen: set[str] = set()
    if not isinstance(workflows, list) or not workflows:
        problems.append("workflows must be a non-empty list")
    else:
        for index, workflow in enumerate(workflows):
            if not isinstance(workflow, dict) or set(workflow) != {"name", "steps"}:
                problems.append(f"workflows[{index}] must have exactly name and steps")
                continue
            name = workflow["name"]
            steps = workflow["steps"]
            if not isinstance(name, str) or _STEP.fullmatch(name) is None:
                problems.append(f"workflows[{index}].name {name!r} is not a step-style name")
            if not isinstance(steps, list) or not steps or not all(
                    isinstance(step, str) and _STEP.fullmatch(step) for step in steps):
                problems.append(f"workflows[{index}].steps {steps!r} are not real step names")
            elif isinstance(steps, list):
                steps_seen.update(steps)
        if not REQUIRED_STEPS <= steps_seen:
            missing = sorted(REQUIRED_STEPS - steps_seen)
            problems.append(f"missing steps {missing}")
    return problems


def test_the_catalog_is_at_least_twenty_four_yaml_files():
    assert CATALOG.is_dir(), f"missing {CATALOG}"
    others = [path.name for path in CATALOG.iterdir()
              if not (path.is_file() and path.suffix == ".yaml")]
    assert others == [], f"catalog holds more than template yaml: {others}"
    files = _yaml_files()
    assert len(files) >= FIRST_CATALOG, f"{len(files)} templates, want at least {FIRST_CATALOG}"


def test_every_template_has_the_catalog_fields():
    files = _yaml_files()
    assert files, "factory_templates has no yaml files"
    failures = []
    for path in files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        problems = _problems(path, doc)
        if problems:
            failures.append(f"{path.name}: {problems}")
    assert not failures, "\n".join(failures)


def test_no_two_templates_share_an_id():
    seen: dict[str, str] = {}
    for path in _yaml_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("id"), str):
            continue
        ident = doc["id"]
        assert ident not in seen, f"{path.name} repeats id {ident}, already used by {seen[ident]}"
        seen[ident] = path.name
    assert len(seen) == len(_yaml_files())


def test_web_maps_game_3d_and_xr_each_have_three():
    counts: Counter[str] = Counter()
    for path in _yaml_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("category"), str):
            counts[doc["category"]] += 1
    short = {category: counts[category] for category, minimum in MINIMUMS.items()
             if counts[category] < minimum}
    assert short == {}, f"category counts under the minimum: {short}"


def test_the_toolchains_here_are_the_ones_the_loader_knows():
    from boss_engine.services import factory_launch, toolchains

    assert set(STACKS_BY_TOOLCHAIN) == set(factory_launch.TEMPLATE_TOOLCHAINS)
    assert set(factory_launch.BUILTIN_TOOLCHAINS) == {"python", "node"}
    opt_in = set(factory_launch.TEMPLATE_TOOLCHAINS) - set(factory_launch.BUILTIN_TOOLCHAINS)
    assert opt_in <= set(toolchains.KNOWN), "a template toolchain BOSS cannot turn on"


def test_no_template_needs_a_platform_the_image_does_not_carry():
    found = []
    for path in _yaml_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for field in ("title", "blurb", "category", "stack", "prompt"):
            hit = UNBUILDABLE.search(str(doc.get(field) or ""))
            if hit:
                found.append(f"{path.name} {field}: {hit.group(0)!r}")
    assert not found, "templates BOSS cannot build:\n" + "\n".join(found)


def test_the_loader_refuses_a_template_without_a_known_toolchain_or_for_android(tmp_path):
    from boss_engine.services import factory_launch

    good = yaml.safe_load((CATALOG / "tile-swap.yaml").read_text(encoding="utf-8"))
    for name, change in (("no-toolchain", {"toolchain": None}),
                         ("rust", {"toolchain": "rust"}),
                         ("android", {"category": "android"}),
                         ("compose", {"stack": "Jetpack Compose"}),
                         ("godot", {"stack": "Godot 4"})):
        doc = {**good, "id": name, **change}
        if change.get("toolchain", "") is None:
            doc.pop("toolchain")
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    (tmp_path / "tile-swap.yaml").write_text(yaml.safe_dump(good), encoding="utf-8")
    listed = factory_launch.list_templates(tmp_path, have=set())
    assert [t["id"] for t in listed["templates"]] == ["tile-swap"]
    assert listed["skipped"] == 5


def test_go_templates_are_marked_unavailable_until_go_is_turned_on():
    from boss_engine.services import factory_launch

    without = {t["id"]: t for t in factory_launch.list_templates(have=set())["templates"]}
    go_ids = {i for i, t in without.items() if t["toolchain"] == "go"}
    assert {"fixture-clock", "license-shelf"} <= go_ids
    for ident, item in without.items():
        if ident in go_ids:
            assert item["available"] is False
            assert item["needs"]["label"] == "Needs Go"
            assert item["needs"]["enable"] == "boss toolchains enable go"
            assert "boss toolchains enable go" in item["needs"]["message"]
        else:
            assert item["available"] is True and item["needs"] is None, ident

    with_go = factory_launch.list_templates(have={"go"})["templates"]
    assert all(t["available"] and t["needs"] is None for t in with_go)


def test_the_listing_reads_the_toolchain_manifest(tmp_path, monkeypatch):
    from boss_engine.services import factory_launch, toolchains

    manifest = tmp_path / "manifest.json"
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))
    manifest.write_text("{}", encoding="utf-8")
    off = {t["id"]: t["available"] for t in factory_launch.list_templates()["templates"]}
    assert off["fixture-clock"] is False and off["tile-swap"] is True
    manifest.write_text('{"go": {"go": "1.27"}}', encoding="utf-8")
    on = {t["id"]: t["available"] for t in factory_launch.list_templates()["templates"]}
    assert on["fixture-clock"] is True


def test_a_launch_of_a_template_that_needs_go_is_refused_in_words(tmp_path, monkeypatch):
    import pytest

    from boss_engine.services import factory_launch, toolchains

    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))
    for prompt in ("", "Make it serve two users."):
        with pytest.raises(factory_launch.FactoryLaunchError) as refused:
            factory_launch.resolve_spec(prompt, "fixture-clock")
        error = refused.value
        assert error.status == 409 and error.code == "toolchain_not_enabled"
        assert error.message.startswith("Not started: Fixture clock is built in Go")
        assert "`boss toolchains enable go`" in error.message
        assert error.body()["message"] == error.message

    # A pasted spec that names Go is held to the same rule.
    pasted = yaml.safe_dump({**yaml.safe_load((CATALOG / "fixture-clock.yaml").read_text()),
                             "id": "pasted-go"})
    with pytest.raises(factory_launch.FactoryLaunchError):
        factory_launch.resolve_spec(pasted, None)

    # Easy Mode with nothing picked never lands on a template this install cannot build.
    spec, origin = factory_launch.resolve_spec("", None)
    assert origin == "lucky" and spec["toolchain"] in ("python", "node")
    assert "available" not in spec and "needs" not in spec

    manifest.write_text('{"go": {"go": "1.27"}}', encoding="utf-8")
    spec, origin = factory_launch.resolve_spec("", "fixture-clock")
    assert origin == "template" and spec["id"] == "fixture-clock" and spec["toolchain"] == "go"
