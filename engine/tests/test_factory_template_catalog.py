"""The factory template catalog is a directory of specs, not a database.

BOSSAPP-1276. The launch path builds whatever these files describe, so a file that adds a field,
drops one, repeats an id, or lets web, android, maps, game, 3d, or xr fall under three templates
fails here. Twenty-four is the first catalog, not a cap. This test only reads the YAML.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import yaml

CATALOG = Path(__file__).resolve().parents[1] / "boss_engine" / "data" / "factory_templates"

# The field set is the contract. A new key is a new catalog, not a quiet extra.
FIELDS = ("id", "title", "blurb", "category", "prompt", "stack", "resources", "workflows")

CATEGORIES = {"web", "android", "maps", "game", "3d", "xr", "data", "internal"}

# The six a launch showcase has to be able to start. data and internal may fill the rest.
MINIMUMS = {"web": 3, "android": 3, "maps": 3, "game": 3, "3d": 3, "xr": 3}

FIRST_CATALOG = 24

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


def test_web_android_maps_game_3d_and_xr_each_have_three():
    counts: Counter[str] = Counter()
    for path in _yaml_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("category"), str):
            counts[doc["category"]] += 1
    short = {category: counts[category] for category, minimum in MINIMUMS.items()
             if counts[category] < minimum}
    assert short == {}, f"category counts under the minimum: {short}"
